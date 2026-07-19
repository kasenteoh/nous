"""Tests for describe-fallback (dry-run probe + persisting apply path).

Pure unit tests (evidence assembly + truncation, the moat-critical descriptor
post-validation, the M1 token-level claim check, the non-US side-finding regex,
wikidata-line formatting, result adjudication — no DB, no LLM) plus a DB-gated
section that exercises cohort selection (including the version gate), an
end-to-end dry run with a fake Wikidata client + fake LLM, guard-rejected-article
exclusion, and the apply path: persistence, idempotency (run twice → second
selects nothing), the never-overwrite guard, stamp-on-null, and
no-stamp-on-LLM-error (skipped without DATABASE_URL).
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
    MAX_LONG_CHARS,
    PROMPT_VERSION,
    DescribeFallbackResult,
)
from nous.pipeline import describe_fallback as df
from nous.pipeline.describe_fallback import (
    CompanySample,
    DescribeFallbackSummary,
    _adjudicate_long,
    _adjudicate_result,
    _assemble_evidence,
    _claim_is_grounded,
    _descriptor_in_evidence,
    _looks_non_us,
    _persist_company,
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


def test_descriptor_generic_or_short_never_grounds() -> None:
    """Review M2: vacuous descriptors match everywhere but license nothing."""
    evidence = "Wikidata description: German holding company (source: url)"
    assert not _descriptor_in_evidence("company", evidence)
    assert not _descriptor_in_evidence("Firm", evidence)
    assert not _descriptor_in_evidence("AI", evidence)  # < 5 chars
    # A real multi-word descriptor still grounds.
    assert _descriptor_in_evidence("holding company", evidence)


def test_descriptor_in_source_url_only_never_grounds() -> None:
    """Review M4: a phrase living only inside a (source: …) URL is not
    editorial evidence."""
    evidence = (
        "Headline about a funding round "
        "(source: https://news.example/ai search engine raises 100m)"
    )
    assert not _descriptor_in_evidence("ai search engine", evidence)


def test_model_validator_nulls_description_without_descriptor() -> None:
    """Review M3: the first defense line, tested directly."""
    result = DescribeFallbackResult(
        description_short="Acme builds rockets.",
        grounding_descriptor=None,
        confidence="high",
        null_reason=None,
    )
    assert result.description_short is None
    assert result.grounding_descriptor is None
    assert result.null_reason == "no_nonfunding_descriptor"


def test_model_validator_nulls_overlong_description() -> None:
    result = DescribeFallbackResult(
        description_short="A" * 300,
        grounding_descriptor="spaceflight company",
        confidence="high",
        null_reason=None,
    )
    assert result.description_short is None
    assert result.null_reason == "insufficient_evidence"


# ── pure: LONG-profile validator gates (2026-07-19.2) ────────────────────────


def test_validator_nulls_long_without_short() -> None:
    """A profile without a tagline is invalid: the descriptor gate nulls the
    short, and the long must go with it (a long can never ride alone)."""
    result = DescribeFallbackResult(
        description_short=None,  # no tagline
        description_long="A grounded two-paragraph profile.",
        grounding_descriptor=None,
        confidence="high",
        null_reason=None,
    )
    assert result.description_short is None
    assert result.description_long is None
    assert result.null_reason == "insufficient_evidence"


def test_validator_nulls_overlong_long_keeps_short() -> None:
    """Over-cap nulls the LONG ONLY — the short tagline stands."""
    result = DescribeFallbackResult(
        description_short="Acme builds rockets.",
        description_long="A" * (MAX_LONG_CHARS + 1),
        grounding_descriptor="spaceflight company",
        confidence="high",
        null_reason=None,
    )
    assert result.description_short == "Acme builds rockets."
    assert result.description_long is None
    assert result.null_reason is None


def test_recordings_compat_without_description_long_key() -> None:
    """#245's live recordings predate description_long and carry no such key;
    the field DEFAULTS to None so they still parse."""
    result = DescribeFallbackResult(
        description_short="Anthropic is an AI safety and research company.",
        grounding_descriptor="AI safety and research company",
        confidence="high",
        null_reason=None,
    )
    assert result.description_long is None
    assert result.description_short == (
        "Anthropic is an AI safety and research company."
    )


def test_adjudicate_described_when_descriptor_grounded() -> None:
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        description_short="SpaceX designs and launches aerospace rockets.",
        grounding_descriptor="aerospace manufacturer",
        confidence="high",
        null_reason=None,
    )
    evidence = (
        "Wikidata description: American aerospace manufacturer that designs, "
        "launches, and builds rockets (source: url)"
    )
    sample, to_persist, _long = _adjudicate_result(
        "SpaceX", "spacex", result, evidence, 3, summary
    )
    assert summary.described == 1
    assert summary.descriptor_not_in_evidence == 0
    assert sample.description_short == "SpaceX designs and launches aerospace rockets."
    assert sample.confidence == "high"
    assert sample.null_reason is None
    # A grounded, non-low, claim-checked description is returned to persist.
    assert to_persist == "SpaceX designs and launches aerospace rockets."


def test_adjudicate_discards_ungrounded_descriptor_echo() -> None:
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        description_short="Acme makes quantum widgets.",
        grounding_descriptor="quantum widget maker",  # NOT in the evidence
        confidence="high",
        null_reason=None,
    )
    evidence = "Wikidata description: an aerospace company (source: url)"
    sample, to_persist, _long = _adjudicate_result(
        "Acme", "acme", result, evidence, 3, summary
    )
    assert summary.described == 0
    assert summary.descriptor_not_in_evidence == 1
    assert sample.description_short is None
    assert sample.null_reason == "descriptor_not_in_evidence"
    assert to_persist is None


def test_long_dies_with_stage_level_short_rejection() -> None:
    """KEY TRAP (review catch): a result carrying BOTH a short and a long must
    lose the long too when the SHORT fails a stage-level check (descriptor not
    in evidence) — the validator never saw the failure, so the stage must
    drop both."""
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        description_short="Acme makes quantum widgets.",
        grounding_descriptor="quantum widget maker",  # NOT in the evidence
        confidence="high",
        null_reason=None,
        description_long=(
            "Acme is a quantum widget maker. It sells widgets to labs."
        ),
    )
    evidence = "Wikidata description: an aerospace company (source: url)"
    _sample, to_persist, long_to_persist = _adjudicate_result(
        "Acme", "acme", result, evidence, 3, summary
    )
    assert to_persist is None
    assert long_to_persist is None
    assert summary.long_written == 0


def test_adjudicate_discards_ungrounded_claim_m1() -> None:
    """M1: descriptor grounds but the sentence as a whole drifts past evidence."""
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        # "aerospace manufacturer" IS in the evidence, but the rest of the
        # sentence (nuclear submarines, ocean drilling) is invented.
        description_short=(
            "Acme is an aerospace manufacturer building nuclear submarines "
            "and autonomous ocean-drilling rigs."
        ),
        grounding_descriptor="aerospace manufacturer",
        confidence="high",
        null_reason=None,
    )
    evidence = "Wikidata description: American aerospace manufacturer (source: url)"
    sample, to_persist, _long = _adjudicate_result(
        "Acme", "acme", result, evidence, 3, summary
    )
    assert summary.described == 0
    assert summary.claims_not_grounded == 1
    assert sample.null_reason == "claims_not_grounded"
    assert to_persist is None


def test_adjudicate_low_confidence_not_persisted() -> None:
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        description_short="Acme builds aerospace parts.",
        grounding_descriptor="aerospace parts",
        confidence="low",
        null_reason=None,
    )
    evidence = "Wikidata description: maker of aerospace parts (source: url)"
    sample, to_persist, _long = _adjudicate_result(
        "Acme", "acme", result, evidence, 3, summary
    )
    assert summary.described == 1
    assert summary.low_confidence == 1
    assert sample.confidence == "low"
    # Described but low-confidence → flagged, never persisted.
    assert to_persist is None


def test_adjudicate_flags_non_us_suspect_but_still_persists() -> None:
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        description_short="Acme is an Indian fintech lending platform.",
        grounding_descriptor="fintech lending platform",
        confidence="high",
        null_reason=None,
    )
    evidence = (
        "Wikidata description: Indian fintech lending platform for consumers "
        "(source: url)"
    )
    sample, to_persist, _long = _adjudicate_result(
        "Acme", "acme-in", result, evidence, 3, summary
    )
    assert summary.described == 1
    assert summary.non_us_suspects == ["acme-in"]
    # Flagged but still returned to persist normally.
    assert to_persist == "Acme is an Indian fintech lending platform."


# ── pure: LONG-profile evidence-proportional gates (2026-07-19.2) ────────────

_LONG_EVIDENCE = (
    "Wikidata description: American aerospace manufacturer that designs, "
    "launches, and builds rockets (source: url)"
)
_GROUNDED_SHORT = "Acme is an American aerospace manufacturer."
_GROUNDED_LONG = (
    "Acme is an American aerospace manufacturer. It designs, launches, and "
    "builds rockets."
)


def _grounded_result_with_long(long: str | None) -> DescribeFallbackResult:
    return DescribeFallbackResult(
        description_short=_GROUNDED_SHORT,
        description_long=long,
        grounding_descriptor="aerospace manufacturer",
        confidence="high",
        null_reason=None,
    )


def test_long_below_evidence_bar_dropped_two_sources() -> None:
    """The rich-evidence bar: < 3 distinct sources drops the LONG, keeps the short."""
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    sample, to_persist, long_to_persist = _adjudicate_result(
        "Acme", "acme", _grounded_result_with_long(_GROUNDED_LONG),
        _LONG_EVIDENCE, 2, summary,
    )
    assert to_persist == _GROUNDED_SHORT  # short survives
    assert long_to_persist is None  # long dropped below the bar
    assert summary.long_below_evidence_bar == 1
    assert summary.long_written == 0
    assert sample.description_long is None


def test_long_accepted_at_three_sources() -> None:
    """At the bar (3 sources) a grounded LONG profile is accepted and persisted."""
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    sample, to_persist, long_to_persist = _adjudicate_result(
        "Acme", "acme", _grounded_result_with_long(_GROUNDED_LONG),
        _LONG_EVIDENCE, 3, summary,
    )
    assert to_persist == _GROUNDED_SHORT
    assert long_to_persist == _GROUNDED_LONG
    assert summary.long_written == 1
    assert summary.long_below_evidence_bar == 0
    assert sample.description_long == _GROUNDED_LONG  # short enough not to truncate


def test_long_claim_grounding_drop_keeps_short() -> None:
    """M1 on the long: a profile drifting past the evidence drops the LONG only."""
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    drifting = (
        "Acme operates deep-sea mining vessels and pharmaceutical laboratories "
        "across three continents."
    )
    sample, to_persist, long_to_persist = _adjudicate_result(
        "Acme", "acme", _grounded_result_with_long(drifting),
        _LONG_EVIDENCE, 3, summary,
    )
    assert to_persist == _GROUNDED_SHORT  # short unaffected
    assert long_to_persist is None
    assert summary.long_claims_not_grounded == 1
    assert summary.long_written == 0


def test_long_not_evaluated_for_low_confidence() -> None:
    """A low-confidence tagline never persists, so its long is not even considered."""
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    result = DescribeFallbackResult(
        description_short=_GROUNDED_SHORT,
        description_long=_GROUNDED_LONG,
        grounding_descriptor="aerospace manufacturer",
        confidence="low",
        null_reason=None,
    )
    sample, to_persist, long_to_persist = _adjudicate_result(
        "Acme", "acme", result, _LONG_EVIDENCE, 5, summary
    )
    assert to_persist is None
    assert long_to_persist is None
    assert summary.long_written == 0
    assert summary.long_below_evidence_bar == 0


def test_adjudicate_long_truncates_sample() -> None:
    """The accepted long is echoed into the sample truncated (yield-table budget)."""
    summary = DescribeFallbackSummary(dry_run=True, prompt_version="t")
    sample = CompanySample(slug="acme")
    # A long, fully-grounded profile: every content word is a repeat of an
    # evidence token, so M1 passes regardless of length.
    long = ("aerospace manufacturer rockets " * 40).strip()
    kept = _adjudicate_long("Acme", long, _LONG_EVIDENCE, 3, sample, summary)
    assert kept == long  # the FULL text is persisted
    assert sample.description_long is not None
    assert len(sample.description_long) <= 205  # truncated for the sample only


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
        sample, to_persist, _long = _adjudicate_result(
            "X", "x", result, "evidence", 3, summary
        )
        assert getattr(summary, attr) == 1
        assert sample.null_reason == reason
        assert to_persist is None


# ── pure: M1 token-level claim check + non-US regex ──────────────────────────


def test_claim_is_grounded_pass_and_fail() -> None:
    evidence = (
        "Wikidata description: conversational search engine that answers "
        "questions with cited sources (source: url)"
    )
    # Content words all present → grounded.
    assert _claim_is_grounded(
        "Perplexity is a conversational search engine answering questions "
        "with cited sources.",
        "Perplexity",
        evidence,
    )
    # A fluent sentence whose content words are mostly absent → not grounded.
    assert not _claim_is_grounded(
        "Perplexity manufactures hydrogen fuel cells for maritime shipping.",
        "Perplexity",
        evidence,
    )


def test_claim_check_ignores_company_name_and_stopwords() -> None:
    # The description repeats the company name and generic verbs; only the real
    # content word ("aerospace") must be found — it is, so this grounds.
    evidence = "Wikidata description: aerospace manufacturer (source: url)"
    assert _claim_is_grounded(
        "Rocketdyne Systems is a company that builds aerospace products.",
        "Rocketdyne Systems",
        evidence,
    )


def test_claim_check_ignores_source_url_tokens() -> None:
    # A content word living only inside the (source: …) URL must not count.
    evidence = (
        "Headline about a raise "
        "(source: https://news.example/biotech-genomics-startup-raises-50m)"
    )
    assert not _claim_is_grounded(
        "Acme is a biotech genomics startup sequencing microbial DNA.",
        "Acme",
        evidence,
    )


def test_looks_non_us_matches_adjectives_and_cities() -> None:
    assert _looks_non_us("An Indian fintech company.")
    assert _looks_non_us("A startup based in Bengaluru.")
    assert _looks_non_us("A London-based analytics firm.")
    assert _looks_non_us("A Tel Aviv cybersecurity company.")
    # US / neutral descriptions do not trip it.
    assert not _looks_non_us("A San Francisco AI search engine.")
    assert not _looks_non_us("An enterprise data platform.")


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
            # Claim-grounded per M1: every content word ("american",
            # "manufacturer") appears in the evidence. A description drifting
            # past the evidence ("builds launch vehicles") is claims_not_grounded
            # — pinned separately in the M1 unit tests.
            return DescribeFallbackResult(
                description_short="Aerospace Co is an American aerospace manufacturer.",
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
        "Aerospace Co is an American aerospace manufacturer."
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


@pytestmark_db
async def test_guard_error_never_stamps(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review catch: a guard LLM ERROR leaves the row un-stamped (re-eligible)
    on BOTH paths — the no-evidence skip AND a null adjudication on partial
    evidence (the dropped article might have described the company)."""
    no_ev = _co("Errored Co")
    partial = _co("Partial Co")
    for c in (no_ev, partial):
        db.add(c)
    await db.flush()
    for c in (no_ev, partial):
        db.add(
            NewsArticle(
                company_id=c.id,
                url=f"https://news.example/{c.slug}",
                title=f"{c.name} in the news",
                source="news.example",
                raw_content="A story the guard errors on.",
            )
        )
    await db.commit()

    # Partial Co gets a wikidata hit (partial evidence survives the guard
    # error); Errored Co gets nothing.
    monkeypatch.setattr(
        df,
        "WikidataClient",
        _fake_wikidata(
            {"Partial Co": _facts("Partial Co", "American software company")}
        ),
    )
    monkeypatch.setattr(
        df,
        "check_article_entity",
        AsyncMock(
            return_value=GuardDecision(
                attach=False, llm_error=True, reason="llm-error"
            )
        ),
    )
    # The LLM nulls the partial-evidence company.
    monkeypatch.setattr(
        df,
        "complete_json",
        AsyncMock(
            return_value=DescribeFallbackResult(
                description_short=None,
                grounding_descriptor=None,
                confidence="low",
                null_reason="insufficient_evidence",
            )
        ),
    )

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)

    assert summary.guard_errors == 2
    for c in (no_ev, partial):
        await db.refresh(c)
        assert c.describe_fallback_prompt_version is None  # re-eligible
        assert c.description_short is None
    # Run again with a healthy guard: both rows re-select (nothing stamped).
    monkeypatch.setattr(
        df,
        "check_article_entity",
        AsyncMock(return_value=GuardDecision(attach=True, reason="ok")),
    )
    summary2 = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)
    assert summary2.cohort_selected == 2


# ── DB-gated: migration 0045 column shape ───────────────────────────────────


@pytestmark_db
async def test_migration_0045_columns_nullable_no_default(db: AsyncSession) -> None:
    """0045 adds description_source + describe_fallback_prompt_version as
    nullable columns with NO default/backfill.

    (There is no alembic up/down test harness in this repo — migrations are
    verified against the CI Postgres already at ``alembic upgrade head`` via
    ORM round-trip, the house pattern used by test_prompt_versioning_db et al.
    This asserts the migration's effect: the columns exist, default to NULL,
    and round-trip a value.)
    """
    co = _co("Fresh Co")
    db.add(co)
    await db.commit()
    await db.refresh(co)
    # No default / backfill: a freshly inserted row has both NULL.
    assert co.description_source is None
    assert co.describe_fallback_prompt_version is None
    # Both are writable and round-trip.
    co.description_source = "fallback"
    co.describe_fallback_prompt_version = PROMPT_VERSION
    await db.commit()
    await db.refresh(co)
    assert co.description_source == "fallback"
    assert co.describe_fallback_prompt_version == PROMPT_VERSION


# ── DB-gated: apply path (persistence, idempotency, guards) ──────────────────


def _grounded_aerospace_llm() -> object:
    """A fake complete_json returning a grounded, claim-checked description for
    an aerospace-evidence prompt, else a model null."""

    async def fake(prompt: str, model: type[DescribeFallbackResult]) -> DescribeFallbackResult:
        if "aerospace" in prompt.lower():
            return DescribeFallbackResult(
                description_short=(
                    "Rocketwerks is an American aerospace manufacturer of "
                    "launch vehicles."
                ),
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

    return fake


@pytestmark_db
async def test_apply_persists_description_source_and_stamp(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply writes description_short + description_source='fallback' + stamp."""
    co = _co("Rocketwerks")
    db.add(co)
    await db.commit()

    monkeypatch.setattr(
        df,
        "WikidataClient",
        _fake_wikidata(
            {
                "Rocketwerks": _facts(
                    "Rocketwerks",
                    "American aerospace manufacturer of launch vehicles",
                )
            }
        ),
    )
    monkeypatch.setattr(df, "complete_json", _grounded_aerospace_llm())

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)

    assert summary.persisted == 1
    assert summary.skipped_already_described == 0
    await db.refresh(co)
    assert co.description_short == (
        "Rocketwerks is an American aerospace manufacturer of launch vehicles."
    )
    assert co.description_source == "fallback"
    assert co.describe_fallback_prompt_version == PROMPT_VERSION


def _grounded_aerospace_llm_with_long() -> object:
    """Like ``_grounded_aerospace_llm`` but also returns a grounded LONG profile
    (every content word lives in the aerospace evidence)."""

    async def fake(
        prompt: str, model: type[DescribeFallbackResult]
    ) -> DescribeFallbackResult:
        if "aerospace" in prompt.lower():
            return DescribeFallbackResult(
                description_short=(
                    "Rocketwerks is an American aerospace manufacturer of "
                    "launch vehicles."
                ),
                description_long=(
                    "Rocketwerks is an American aerospace manufacturer of "
                    "launch vehicles. It designs and builds rockets."
                ),
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

    return fake


@pytestmark_db
async def test_apply_persists_long_profile_on_rich_evidence(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """>= 3 distinct evidence sources (wikidata + two surviving articles) → the
    grounded LONG profile is written alongside the short + provenance."""
    co = _co("Rocketwerks")
    db.add(co)
    await db.flush()
    for n in range(2):
        db.add(
            NewsArticle(
                company_id=co.id,
                url=f"https://outlet-{n}.example/rocketwerks-{n}",
                title="Rocketwerks builds rockets",
                # Distinct OUTLET names — the bar counts these (URL host is
                # the fallback), so three independent voices are three.
                source=f"outlet-{n}.example",
                raw_content=(
                    "Rocketwerks, the aerospace manufacturer, designs and "
                    "builds launch vehicles and rockets."
                ),
            )
        )
    await db.commit()

    monkeypatch.setattr(
        df,
        "WikidataClient",
        _fake_wikidata(
            {
                "Rocketwerks": _facts(
                    "Rocketwerks",
                    "American aerospace manufacturer of launch vehicles",
                )
            }
        ),
    )
    monkeypatch.setattr(df, "complete_json", _grounded_aerospace_llm_with_long())

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)

    assert summary.persisted == 1
    assert summary.long_written == 1
    await db.refresh(co)
    assert co.description_short == (
        "Rocketwerks is an American aerospace manufacturer of launch vehicles."
    )
    assert co.description_long == (
        "Rocketwerks is an American aerospace manufacturer of launch "
        "vehicles. It designs and builds rockets."
    )
    assert co.description_source == "fallback"


@pytestmark_db
async def test_gn_syndicated_outlets_clear_the_evidence_bar(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blue-origin shape (profile-run-1 catch): Google News syndication
    stores news.google.com URLs for EVERY outlet, so host-based counting saw
    one source and wrote zero longs. The bar counts the stored OUTLET name
    first — three distinct outlets behind one syndication host are three
    voices."""
    co = _co("Rocketwerks")
    db.add(co)
    await db.flush()
    for n, outlet in enumerate(["The Motley Fool", "Yahoo Finance", "Reuters"]):
        db.add(
            NewsArticle(
                company_id=co.id,
                url=f"https://news.google.com/rss/articles/rocketwerks-{n}",
                title="Rocketwerks builds rockets",
                source=outlet,
                raw_content=(
                    "Rocketwerks, the aerospace manufacturer, designs and "
                    "builds launch vehicles and rockets."
                ),
            )
        )
    await db.commit()

    monkeypatch.setattr(df, "WikidataClient", _fake_wikidata({}))
    monkeypatch.setattr(
        df,
        "check_article_entity",
        AsyncMock(return_value=GuardDecision(attach=True, reason="ok")),
    )
    monkeypatch.setattr(df, "complete_json", _grounded_aerospace_llm_with_long())

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)

    assert summary.long_written == 1
    assert summary.long_below_evidence_bar == 0
    await db.refresh(co)
    assert co.description_long is not None


@pytestmark_db
async def test_apply_refreshes_own_fallback_stopgap(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row carrying THIS stage's own fallback stopgap at an OLDER prompt
    version re-selects and its description is refreshed (the widened
    WHERE (description_short IS NULL OR description_source='fallback'))."""
    co = _co(
        "Rocketwerks",
        description_short="Stale fallback tagline.",
        description_source="fallback",
        describe_fallback_prompt_version="2026-01-01.1",
    )
    db.add(co)
    await db.commit()

    monkeypatch.setattr(
        df,
        "WikidataClient",
        _fake_wikidata(
            {
                "Rocketwerks": _facts(
                    "Rocketwerks",
                    "American aerospace manufacturer of launch vehicles",
                )
            }
        ),
    )
    monkeypatch.setattr(df, "complete_json", _grounded_aerospace_llm())

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)

    assert summary.cohort_selected == 1
    assert summary.persisted == 1  # refreshed, not skipped
    await db.refresh(co)
    assert co.description_short == (
        "Rocketwerks is an American aerospace manufacturer of launch vehicles."
    )
    assert co.description_source == "fallback"
    assert co.describe_fallback_prompt_version == PROMPT_VERSION


@pytestmark_db
async def test_distrust_null_clears_stale_fallback(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review catch: a fallback row re-adjudicated to a DISTRUST-class null
    (entity_ambiguity here) loses its stale description — text the current
    version won't stand behind must not keep rendering."""
    co = _co(
        "Ambiguoco",
        description_short="Stale fallback tagline.",
        description_long="Stale fallback prose.",
        description_source="fallback",
        describe_fallback_prompt_version="2026-01-01.1",
    )
    db.add(co)
    await db.commit()

    monkeypatch.setattr(
        df,
        "WikidataClient",
        _fake_wikidata(
            {"Ambiguoco": _facts("Ambiguoco", "American software company")}
        ),
    )
    monkeypatch.setattr(
        df,
        "complete_json",
        AsyncMock(
            return_value=DescribeFallbackResult(
                description_short=None,
                grounding_descriptor=None,
                confidence="low",
                null_reason="entity_ambiguity",
            )
        ),
    )

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)

    assert summary.stale_cleared == 1
    await db.refresh(co)
    assert co.description_short is None
    assert co.description_long is None
    assert co.description_source is None
    assert co.describe_fallback_prompt_version == PROMPT_VERSION


@pytestmark_db
async def test_availability_null_keeps_stale_fallback(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retention half (intentional): an AVAILABILITY-class null
    (insufficient_evidence — e.g. a transient wikidata miss thinned the
    evidence) keeps the previously-grounded description; only the stamp
    advances."""
    co = _co(
        "Keepco",
        description_short="Previously grounded tagline.",
        description_source="fallback",
        describe_fallback_prompt_version="2026-01-01.1",
    )
    db.add(co)
    await db.flush()
    db.add(
        NewsArticle(
            company_id=co.id,
            url="https://news.example/keepco",
            title="Keepco raises a round",
            source="news.example",
            raw_content="Keepco announced a financing round today.",
        )
    )
    await db.commit()

    monkeypatch.setattr(df, "WikidataClient", _fake_wikidata({}))
    monkeypatch.setattr(
        df,
        "check_article_entity",
        AsyncMock(return_value=GuardDecision(attach=True, reason="ok")),
    )
    monkeypatch.setattr(
        df,
        "complete_json",
        AsyncMock(
            return_value=DescribeFallbackResult(
                description_short=None,
                grounding_descriptor=None,
                confidence="low",
                null_reason="insufficient_evidence",
            )
        ),
    )

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)

    assert summary.stale_cleared == 0
    await db.refresh(co)
    assert co.description_short == "Previously grounded tagline."
    assert co.description_source == "fallback"
    assert co.describe_fallback_prompt_version == PROMPT_VERSION



@pytestmark_db
async def test_apply_is_idempotent_second_run_selects_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run twice: the second run selects nothing (described row now has a
    description; a null row is version-stamped) and writes nothing."""
    described = _co("Rocketwerks")
    # A null-yielding company with an article (so it reaches the LLM and gets a
    # deliberate null → stamped, description_short still NULL).
    null_co = _co("Null Co")
    for c in (described, null_co):
        db.add(c)
    await db.flush()
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
            {
                "Rocketwerks": _facts(
                    "Rocketwerks",
                    "American aerospace manufacturer of launch vehicles",
                )
            }
        ),
    )
    monkeypatch.setattr(df, "complete_json", _grounded_aerospace_llm())

    first = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)
    assert first.cohort_selected == 2
    assert first.persisted == 1
    assert first.null_insufficient == 1

    second = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)
    # Described row excluded (description_short set); null row excluded (stamped
    # at the current version). Nothing re-selected, nothing re-billed.
    assert second.cohort_selected == 0
    assert second.persisted == 0
    assert second.llm_calls == 0


@pytestmark_db
async def test_apply_stamps_null_without_writing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model-null company is stamped (so it isn't re-billed) but its
    description_short / description_source stay NULL."""
    null_co = _co("Null Co")
    db.add(null_co)
    await db.flush()
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

    monkeypatch.setattr(df, "WikidataClient", _fake_wikidata({}))
    monkeypatch.setattr(df, "complete_json", _grounded_aerospace_llm())

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)

    assert summary.persisted == 0
    assert summary.null_insufficient == 1
    await db.refresh(null_co)
    assert null_co.description_short is None
    assert null_co.description_source is None
    assert null_co.describe_fallback_prompt_version == PROMPT_VERSION


@pytestmark_db
async def test_version_gate_excludes_already_stamped_row(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A residue row already stamped at the current PROMPT_VERSION is not
    re-selected; one stamped at an older version still is."""
    current = _co("Current Stamp", describe_fallback_prompt_version=PROMPT_VERSION)
    older = _co("Older Stamp", describe_fallback_prompt_version="2026-01-01.1")
    for c in (current, older):
        db.add(c)
    await db.commit()

    monkeypatch.setattr(df, "WikidataClient", _fake_wikidata({}))
    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=True)

    slugs = [s.slug for s in summary.samples]
    assert current.slug not in slugs
    assert older.slug in slugs


@pytestmark_db
async def test_no_stamp_on_llm_error(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-company LLM error leaves the company un-stamped (re-eligible)."""
    co = _co("Erroring Co")
    db.add(co)
    await db.commit()

    monkeypatch.setattr(
        df,
        "WikidataClient",
        _fake_wikidata({"Erroring Co": _facts("Erroring Co", "a robotics company")}),
    )
    monkeypatch.setattr(
        df, "complete_json", AsyncMock(side_effect=LLMError("boom"))
    )

    summary = await run_describe_fallback(db, user_agent=_UA, limit=20, dry_run=False)

    assert summary.errors == 1
    assert summary.persisted == 0
    await db.refresh(co)
    assert co.describe_fallback_prompt_version is None


@pytestmark_db
async def test_persist_company_never_overwrites_existing_description(
    db: AsyncSession,
) -> None:
    """The never-overwrite guard: a fresh read finds description_short already
    set (a concurrent enrich mid-run) → skip the write, but still stamp."""
    co = _co("Raced Co")
    db.add(co)
    await db.commit()
    # Simulate the enrich cron describing the row after selection saw it NULL.
    co.description_short = "An enrich-written own-website description."
    co.description_source = None  # own-website path records no fallback source
    await db.commit()

    summary = DescribeFallbackSummary(dry_run=False, prompt_version=PROMPT_VERSION)
    await _persist_company(
        db, co, "A fallback description that must NOT win.", None, summary
    )

    assert summary.persisted == 0
    assert summary.skipped_already_described == 1
    await db.refresh(co)
    # The enrich description survives; describe-fallback did not clobber it.
    assert co.description_short == "An enrich-written own-website description."
    assert co.description_source is None
    # But it IS stamped (a completed adjudication) so it isn't re-billed.
    assert co.describe_fallback_prompt_version == PROMPT_VERSION
