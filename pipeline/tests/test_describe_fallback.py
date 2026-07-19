"""Tests for the describe-fallback probe (dry-run only).

Pure unit tests (evidence assembly + truncation, the moat-critical descriptor
post-validation, wikidata-line formatting, result adjudication — no DB, no LLM)
plus a DB-gated section that exercises cohort selection, an end-to-end dry run
with a fake Wikidata client + fake LLM (a described sample and a null sample),
and guard-rejected-article exclusion (skipped without DATABASE_URL).

NOTE (follow-up): this probe does NOT register a golden set — the eval registry
addition lands with the persisting apply PR, once the prompt is locked by the
prod dry run. See BACKLOG "missing-data residue".
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle, RawPage
from nous.llm.client import LLMError
from nous.llm.prompts.describe_fallback import (
    MAX_EVIDENCE_CHARS,
    DescribeFallbackResult,
)
from nous.pipeline import describe_fallback as df
from nous.pipeline.describe_fallback import (
    DescribeFallbackSummary,
    _adjudicate_result,
    _assemble_evidence,
    _descriptor_in_evidence,
    _wikidata_lines,
    run_describe_fallback,
)
from nous.pipeline.entity_guard import GuardDecision
from nous.sources.wikidata import WikidataFacts
from nous.util.slugify import normalize_name

_UA = "nous-test (test@example.com)"


# ── pure: evidence assembly + truncation ─────────────────────────────────────


def test_assemble_evidence_orders_wikidata_first() -> None:
    evidence = _assemble_evidence(
        ["Wikidata description: A rocket company (source: url)"],
        ["Headline (source: art)\nExcerpt text"],
    )
    assert evidence.index("Wikidata description") < evidence.index("Headline")


def test_assemble_evidence_truncates_to_budget() -> None:
    huge = ["x" * (MAX_EVIDENCE_CHARS * 2)]
    evidence = _assemble_evidence(huge, [])
    assert len(evidence) <= MAX_EVIDENCE_CHARS


def test_wikidata_lines_description_first_each_cites_source() -> None:
    facts = WikidataFacts(
        qid="Q1",
        entity_url="https://www.wikidata.org/wiki/Q1",
        matched_label="SpaceX",
        entity_description="American aerospace manufacturer",
        inception_year=2002,
        hq=["Hawthorne, California"],
        industries=["aerospace"],
        founders=["Elon Musk"],
    )
    lines = _wikidata_lines(facts)
    assert lines[0].startswith("Wikidata description: American aerospace manufacturer")
    assert all("https://www.wikidata.org/wiki/Q1" in ln for ln in lines)
    # An empty facts object contributes nothing.
    empty = WikidataFacts(
        qid="Q2", entity_url="https://www.wikidata.org/wiki/Q2", matched_label="X"
    )
    assert _wikidata_lines(empty) == []


# ── pure: descriptor post-validation (the moat check) ────────────────────────


def test_descriptor_in_evidence_present_and_absent() -> None:
    evidence = "Wikidata description: American aerospace manufacturer (source: url)"
    # Present, whitespace-insensitive + case-insensitive.
    assert _descriptor_in_evidence("Aerospace   Manufacturer", evidence)
    # Absent.
    assert not _descriptor_in_evidence("sodium-ion battery maker", evidence)
    # Empty / None never grounds.
    assert not _descriptor_in_evidence(None, evidence)
    assert not _descriptor_in_evidence("   ", evidence)


def test_adjudicate_described_when_descriptor_grounded() -> None:
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        description_short="SpaceX designs and launches rockets.",
        grounding_descriptor="aerospace manufacturer",
        confidence="high",
        null_reason=None,
    )
    evidence = "Wikidata description: American aerospace manufacturer (source: url)"
    sample = _adjudicate_result("spacex", result, evidence, summary)
    assert summary.described == 1
    assert summary.descriptor_not_in_evidence == 0
    assert sample.description_short == "SpaceX designs and launches rockets."
    assert sample.confidence == "high"
    assert sample.null_reason is None


def test_adjudicate_discards_ungrounded_descriptor_echo() -> None:
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        description_short="Acme makes quantum widgets.",
        grounding_descriptor="quantum widget maker",  # NOT in the evidence
        confidence="high",
        null_reason=None,
    )
    evidence = "Wikidata description: an aerospace company (source: url)"
    sample = _adjudicate_result("acme", result, evidence, summary)
    assert summary.described == 0
    assert summary.descriptor_not_in_evidence == 1
    assert sample.description_short is None
    assert sample.null_reason == "descriptor_not_in_evidence"


def test_adjudicate_low_confidence_counted_separately() -> None:
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        description_short="Acme builds aerospace parts.",
        grounding_descriptor="aerospace",
        confidence="low",
        null_reason=None,
    )
    sample = _adjudicate_result("acme", result, "aerospace company", summary)
    assert summary.described == 1
    assert summary.low_confidence == 1
    assert sample.confidence == "low"


def test_adjudicate_maps_null_reasons() -> None:
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    for reason, attr in (
        ("no_nonfunding_descriptor", "null_no_descriptor"),
        ("insufficient_evidence", "null_insufficient"),
        ("entity_ambiguity", "null_ambiguity"),
    ):
        result = DescribeFallbackResult(
            description_short=None,
            grounding_descriptor=None,
            confidence="low",
            null_reason=reason,  # type: ignore[arg-type]
        )
        sample = _adjudicate_result("x", result, "evidence", summary)
        assert getattr(summary, attr) == 1
        assert sample.null_reason == reason


def test_apply_mode_raises() -> None:
    import asyncio

    async def _go() -> None:
        with pytest.raises(ValueError, match="apply path not built"):
            await run_describe_fallback(
                AsyncMock(), user_agent=_UA, limit=1, dry_run=False
            )

    asyncio.run(_go())


# ── DB-gated: cohort selection + end-to-end dry run ──────────────────────────

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _co(name: str, **kwargs: object) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{normalize_name(name) or 'company'}-{suffix}",
        normalized_name=normalize_name(name),
        **kwargs,  # type: ignore[arg-type]
    )


def _fake_wikidata(facts_by_name: dict[str, WikidataFacts | None]) -> type:
    """A WikidataClient stand-in returning canned facts keyed by company name."""

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def __aenter__(self) -> _Fake:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def entity_facts(
            self, name: str, *, company_country: str | None = None, limit: int = 5
        ) -> WikidataFacts | None:
            return facts_by_name.get(name)

    return _Fake


def _facts(name: str, description: str) -> WikidataFacts:
    return WikidataFacts(
        qid="Q1",
        entity_url="https://www.wikidata.org/wiki/Q1",
        matched_label=name,
        entity_description=description,
    )


@pytestmark_db
async def test_cohort_selects_only_unscrapable_residue(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shown + description-less + no readable own page; prominence-ordered."""
    from decimal import Decimal

    residue_big = _co("Residue Big", latest_round_amount=Decimal("500000000"))
    residue_small = _co("Residue Small", latest_round_amount=Decimal("1000000"))
    has_desc = _co("Has Desc", description_short="An AI company.")
    has_long = _co("Has Long", description_long="A long description.")
    excluded = _co("Foreign Co", exclusion_reason="non_us")
    has_pages = _co("Has Pages")
    for c in (residue_big, residue_small, has_desc, has_long, excluded, has_pages):
        db.add(c)
    await db.flush()
    # has_pages owns a real (>=200 char) raw_page → NOT residue.
    db.add(
        RawPage(company_id=has_pages.id, url="https://x.example/", content="y" * 300)
    )
    await db.commit()

    monkeypatch.setattr(df, "WikidataClient", _fake_wikidata({}))
    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=True)

    slugs = [s.slug for s in summary.samples]
    assert residue_big.slug in slugs
    assert residue_small.slug in slugs
    assert has_desc.slug not in slugs
    assert has_long.slug not in slugs
    assert excluded.slug not in slugs
    assert has_pages.slug not in slugs
    # Prominence order: the bigger raise comes first.
    assert slugs.index(residue_big.slug) < slugs.index(residue_small.slug)


@pytestmark_db
async def test_dry_run_described_and_null_samples(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end dry run: one grounded description + one model-null, no writes."""
    from decimal import Decimal

    described_co = _co("Aerospace Co", latest_round_amount=Decimal("900000000"))
    null_co = _co("Null Co", latest_round_amount=Decimal("100000000"))
    for c in (described_co, null_co):
        db.add(c)
    await db.flush()
    # null_co has an article (so it has evidence and reaches the LLM); no
    # wikidata hit for it.
    db.add(
        NewsArticle(
            company_id=null_co.id,
            url="https://news.example/null-co",
            title="Null Co raises a round",
            source="news.example",
            raw_content="Null Co announced a financing round today.",
        )
    )
    await db.commit()

    monkeypatch.setattr(
        df,
        "WikidataClient",
        _fake_wikidata(
            {"Aerospace Co": _facts("Aerospace Co", "American aerospace manufacturer")}
        ),
    )

    async def fake_complete_json(
        prompt: str, model: type[DescribeFallbackResult]
    ) -> DescribeFallbackResult:
        if "aerospace" in prompt.lower():
            return DescribeFallbackResult(
                description_short="Aerospace Co builds launch vehicles.",
                grounding_descriptor="aerospace manufacturer",
                confidence="high",
                null_reason=None,
            )
        return DescribeFallbackResult(
            description_short=None,
            grounding_descriptor=None,
            confidence="low",
            null_reason="insufficient_evidence",
        )

    monkeypatch.setattr(df, "complete_json", fake_complete_json)

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=True)

    assert summary.cohort_selected == 2
    assert summary.wikidata_hits == 1
    assert summary.llm_calls == 2
    assert summary.described == 1
    assert summary.null_insufficient == 1
    by_slug = {s.slug: s for s in summary.samples}
    assert by_slug[described_co.slug].description_short == (
        "Aerospace Co builds launch vehicles."
    )
    assert by_slug[described_co.slug].wikidata is True
    assert by_slug[null_co.slug].description_short is None
    assert by_slug[null_co.slug].null_reason == "insufficient_evidence"

    # Read-only: nothing persisted.
    await db.refresh(described_co)
    assert described_co.description_short is None


@pytestmark_db
async def test_guard_rejected_article_excluded_from_evidence(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard-dropped article yields no evidence; with no wikidata hit the
    company is skipped without an LLM call."""
    co = _co("Guarded Co")
    db.add(co)
    await db.flush()
    db.add(
        NewsArticle(
            company_id=co.id,
            url="https://news.example/guarded",
            title="Guarded Co in the news",
            source="news.example",
            raw_content="A story that the guard will reject as a different entity.",
        )
    )
    await db.commit()

    monkeypatch.setattr(df, "WikidataClient", _fake_wikidata({}))
    monkeypatch.setattr(
        df,
        "check_article_entity",
        AsyncMock(
            return_value=GuardDecision(attach=False, reason="llm-mismatch")
        ),
    )
    llm = AsyncMock(side_effect=LLMError("should not be called"))
    monkeypatch.setattr(df, "complete_json", llm)

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=True)

    assert summary.articles_seen == 1
    assert summary.guard_dropped == 1
    assert summary.skipped_no_evidence == 1
    assert summary.llm_calls == 0
    llm.assert_not_awaited()
