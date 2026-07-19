"""Integration tests for the enrich-companies pipeline stage.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via ``alembic upgrade head``.

Tests are skipped when DATABASE_URL is unset or empty.

``complete_json`` is monkeypatched with a schema-routing fake (the judge
call returns a canned CompanyDescription, the describe call a canned
CompanyLongDescription) so no real LLM calls are made — mirroring the W-F
two-call design.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Person, RawPage
from nous.llm.client import LLMParseError, LLMRateLimitError
from nous.llm.prompts.company_description import (
    PROMPT_VERSION as JUDGE_VERSION,
)
from nous.llm.prompts.company_description import CompanyDescription, PersonExtraction
from nous.llm.prompts.company_description_long import (
    PROMPT_VERSION as DESCRIBE_VERSION,
)
from nous.llm.prompts.company_description_long import CompanyLongDescription
from nous.pipeline.enrich_companies import (
    run_enrich_companies,
    run_redescribe_outdated,
)

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CANNED_JUDGE = CompanyDescription(
    description_short="A short description of the company.",
    primary_category="developer tools",
    tags=["open source", "API first", "cloud native"],
    website_state="ok",
)

_CANNED_LONG_TEXT = (
    "Paragraph one about the problem and the product.\n\n"
    "Paragraph two about who uses it and how."
)
_CANNED_LONG = CompanyLongDescription(description_long=_CANNED_LONG_TEXT)


def _llm_router(
    judge: CompanyDescription | Exception = _CANNED_JUDGE,
    describe: CompanyLongDescription | Exception = _CANNED_LONG,
) -> tuple[Any, dict[str, int]]:
    """A ``complete_json`` stand-in that routes by response schema.

    The real client guarantees the return type matches the requested schema,
    so a faithful mock must too — returning a CompanyDescription to the
    describe call would mask real bugs. Exceptions are raised instead of
    returned. The second element counts calls per prompt.
    """
    calls = {"judge": 0, "describe": 0}

    async def _fake(prompt: str, schema: type, **kwargs: Any) -> Any:
        if schema is CompanyDescription:
            calls["judge"] += 1
            result: Any = judge
        else:
            assert schema is CompanyLongDescription, f"unexpected schema {schema}"
            calls["describe"] += 1
            result = describe
        if isinstance(result, Exception):
            raise result
        return result

    return _fake, calls


def _make_company(
    *,
    name: str = "Acme Inc.",
    slug: str = "acme",
    description_short: str | None = None,
    last_enriched_at: datetime | None = None,
    latest_round_amount: Decimal | None = None,
    funding_round_count: int = 0,
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        description_short=description_short,
        last_enriched_at=last_enriched_at,
        latest_round_amount=latest_round_amount,
        funding_round_count=funding_round_count,
    )


# Visible text ~880 chars: clears BOTH _MIN_TEXT_CHARS (200) and
# _MIN_DESCRIBE_CHARS (700), so the default page exercises the full
# judge + describe flow. Thin-describe tests build their own shorter page.
_RICH_PAGE_HTML = (
    "<html><body><p>This is a substantial enough page to pass the minimum text check. "
    "The company builds developer tools for API-first teams. "
    "Their platform enables engineers to design, test, and deploy APIs at scale. "
    "Founded in 2021, they serve hundreds of enterprise customers globally. "
    "Their flagship product is a cloud-native API gateway with built-in observability. "
    "The team is distributed across North America and Europe. "
    "The gateway terminates traffic close to users and applies rate limits, "
    "authentication, and schema validation before requests reach upstream services. "
    "A control plane manages configuration as code, with previews for every change "
    "and automatic rollback when error rates rise. Customers integrate through "
    "declarative manifests checked into their own repositories, and usage-based "
    "pricing scales from side projects to large enterprise deployments.</p></body></html>"
)


def _make_raw_page(company_id: Any, *, url: str = "https://acme.com/") -> RawPage:
    return RawPage(company_id=company_id, url=url, content=_RICH_PAGE_HTML)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_enrich_supersedes_fallback_description_provenance(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blue-origin trajectory (0045 handoff): a row carrying a
    describe-fallback description loses the 'fallback' provenance when the
    own-website enrich path writes its description — otherwise the own-site
    text would inherit stale third-party gating/attribution."""
    company = _make_company(slug="enrich-fallback-handoff")
    company.description_short = "Acme is an American aerospace manufacturer."
    company.description_long = "Stale fallback prose that must not survive."
    company.description_source = "fallback"
    db.add(company)
    await db.flush()
    page = _make_raw_page(company.id)
    db.add(page)
    await db.flush()
    await db.commit()

    fake, _calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    await run_enrich_companies(db)

    await db.refresh(company)
    assert company.description_short == _CANNED_JUDGE.description_short
    assert company.description_source is None  # own-website provenance
    # The fallback-written LONG was cleared before the own-site describe ran
    # (review catch): whatever stands now is own-site text or nothing — never
    # the stale third-party prose under the own-site attribution.
    assert company.description_long != "Stale fallback prose that must not survive."


async def test_enrich_populates_company_fields(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful enrichment populates all description fields on the company."""
    company = _make_company(slug="enrich-basic")
    db.add(company)
    await db.flush()
    page = _make_raw_page(company.id)
    db.add(page)
    await db.flush()
    await db.commit()

    fake, _calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db)

    await db.refresh(company)
    assert company.description_short == _CANNED_JUDGE.description_short
    # description_long comes from the SECOND (describe) call, not the judge.
    assert company.description_long == _CANNED_LONG_TEXT
    assert company.primary_category == _CANNED_JUDGE.primary_category
    assert company.last_enriched_at is not None
    # Provenance: eligibility carries the judge version, enrichment the
    # describe version (see enrich_companies module docstring).
    assert company.eligibility_prompt_version == JUDGE_VERSION
    assert company.enrichment_prompt_version == DESCRIBE_VERSION
    assert summary.companies_enriched >= 1
    assert summary.descriptions_written >= 1
    assert _calls == {"judge": 1, "describe": 1}


async def test_enrich_writes_people(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """People returned by the LLM are written to the people table, ranked, and
    attributed to the company website."""
    company = _make_company(slug="enrich-people")
    company.website = "https://acme.example/"
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id))
    await db.flush()
    await db.commit()

    canned = CompanyDescription(
        description_short="Short.",
        primary_category="developer tools",
        tags=[],
        website_state="ok",
        people=[
            PersonExtraction(name="Ada Lovelace", role="CEO"),
            PersonExtraction(name="Alan Turing", role="CTO"),
        ],
    )
    fake, _calls = _llm_router(judge=canned)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db)
    assert summary.people_written == 2

    rows = (
        await db.execute(
            select(Person).where(Person.company_id == company.id).order_by(Person.rank)
        )
    ).scalars().all()
    assert [(r.name, r.role, r.rank) for r in rows] == [
        ("Ada Lovelace", "CEO", 1),
        ("Alan Turing", "CTO", 2),
    ]
    assert all(r.source_url == "https://acme.example/" for r in rows)


async def test_tags_are_normalized(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tags are lowercased and whitespace is replaced with hyphens."""
    company = _make_company(slug="enrich-tags")
    db.add(company)
    await db.flush()
    page = _make_raw_page(company.id)
    db.add(page)
    await db.flush()
    await db.commit()

    canned = CompanyDescription(
        description_short="Short.",
        primary_category="fintech",
        tags=["Open Source", "API First", "Cloud Native"],
        website_state="ok",
    )
    fake, _calls = _llm_router(judge=canned)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    await run_enrich_companies(db)

    await db.refresh(company)
    assert company.tags == ["open-source", "api-first", "cloud-native"]


async def test_last_enriched_payload_round_trips(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """last_enriched_payload stores the CompanyDescription as a JSON-compatible dict."""
    company = _make_company(slug="enrich-payload")
    db.add(company)
    await db.flush()
    page = _make_raw_page(company.id)
    db.add(page)
    await db.flush()
    await db.commit()

    fake, _calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    await run_enrich_companies(db)

    await db.refresh(company)
    assert company.last_enriched_payload is not None
    expected_short = _CANNED_JUDGE.description_short
    assert company.last_enriched_payload["description_short"] == expected_short
    expected_cat = _CANNED_JUDGE.primary_category
    assert company.last_enriched_payload["primary_category"] == expected_cat
    # The payload keeps the historical single-call shape: the describe
    # call's output is folded in under description_long.
    assert company.last_enriched_payload["description_long"] == _CANNED_LONG_TEXT
    # tags should be a list of strings in the payload.
    assert isinstance(company.last_enriched_payload["tags"], list)


async def test_rate_limit_stops_loop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLMRateLimitError stops the loop; no further companies are enriched."""
    # Create two companies, both eligible for enrichment.
    company1 = _make_company(name="RateLimit Co 1 Inc.", slug="rl-co-1")
    company2 = _make_company(name="RateLimit Co 2 Inc.", slug="rl-co-2")
    db.add_all([company1, company2])
    await db.flush()
    page1 = _make_raw_page(company1.id, url="https://rl1.com/")
    page2 = _make_raw_page(company2.id, url="https://rl2.com/")
    db.add_all([page1, page2])
    await db.flush()
    await db.commit()

    fake, calls = _llm_router(judge=LLMRateLimitError("rate limited"))
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db)

    # The judge call should have run exactly once; the loop stopped after the
    # first rate limit, and the describe call was never reached.
    assert calls == {"judge": 1, "describe": 0}
    assert summary.skipped_rate_limited == 1
    assert summary.companies_enriched == 0


async def test_llm_parse_error_continues_loop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLMParseError increments llm_failures but continues to the next company."""
    company1 = _make_company(name="ParseErr Co 1 Inc.", slug="pe-co-1")
    company2 = _make_company(name="ParseErr Co 2 Inc.", slug="pe-co-2")
    db.add_all([company1, company2])
    await db.flush()
    page1 = _make_raw_page(company1.id, url="https://pe1.com/")
    page2 = _make_raw_page(company2.id, url="https://pe2.com/")
    db.add_all([page1, page2])
    await db.flush()
    await db.commit()

    judge_calls = 0

    async def _fail_first_judge(prompt: str, schema: type, **kwargs: Any) -> Any:
        nonlocal judge_calls
        if schema is CompanyDescription:
            judge_calls += 1
            if judge_calls == 1:
                raise LLMParseError("parse failed")
            return _CANNED_JUDGE
        assert schema is CompanyLongDescription
        return _CANNED_LONG

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        _fail_first_judge,
    )

    summary = await run_enrich_companies(db)

    assert judge_calls == 2
    assert summary.llm_failures == 1
    assert summary.companies_enriched == 1


async def test_company_with_no_raw_pages_is_skipped(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companies without any raw_pages are not passed to the LLM."""
    company = _make_company(slug="enrich-nopages")
    db.add(company)
    await db.flush()
    # No raw_pages added.
    await db.commit()

    mock_complete_json = AsyncMock(return_value=_CANNED_JUDGE)
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        mock_complete_json,
    )

    summary = await run_enrich_companies(db)

    mock_complete_json.assert_not_called()
    assert summary.companies_seen == 0  # filtered before even entering the loop


async def test_already_enriched_recently_is_skipped(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companies enriched within refetch_after_days are skipped."""
    now = datetime.now(tz=UTC)
    company = _make_company(
        slug="enrich-recent",
        description_short="Already set.",
        last_enriched_at=now - timedelta(days=1),
    )
    db.add(company)
    await db.flush()
    page = _make_raw_page(company.id)
    db.add(page)
    await db.flush()
    await db.commit()

    mock_complete_json = AsyncMock(return_value=_CANNED_JUDGE)
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        mock_complete_json,
    )

    summary = await run_enrich_companies(db, refetch_after_days=90)

    mock_complete_json.assert_not_called()
    assert summary.companies_seen == 0


async def test_stale_enrichment_triggers_rerun(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companies enriched before refetch_after_days ago are re-enriched."""
    old = datetime.now(tz=UTC) - timedelta(days=200)
    company = _make_company(
        slug="enrich-stale",
        description_short="Old description.",
        last_enriched_at=old,
    )
    db.add(company)
    await db.flush()
    page = _make_raw_page(company.id)
    db.add(page)
    await db.flush()
    await db.commit()

    fake, _calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db, refetch_after_days=90)

    assert summary.companies_enriched >= 1
    await db.refresh(company)
    assert company.description_short == _CANNED_JUDGE.description_short
    assert company.description_long == _CANNED_LONG_TEXT


async def test_max_companies_caps_enrichment(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_companies limits how many companies are enriched."""
    for i in range(3):
        company = _make_company(
            name=f"MaxLimit Co {i} Inc.",
            slug=f"maxlimit-enrich-{i}",
        )
        db.add(company)
        await db.flush()
        page = _make_raw_page(company.id, url=f"https://maxlimit{i}.com/")
        db.add(page)
    await db.flush()
    await db.commit()

    fake, _calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db, max_companies=1)

    assert summary.companies_seen == 1
    assert summary.companies_enriched == 1


async def test_max_companies_prioritizes_highest_raise(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a tight --max-companies, the highest-raise eligible companies are
    enriched first. This is the fix for marquee companies (Perplexity, Mistral,
    …) showing up as blank husks at the top of "Largest raise": they have
    funding + scraped pages but a small per-run budget kept skipping them."""
    big = _make_company(
        name="BigRaise Inc.",
        slug="bigraise-prio-enrich",
        latest_round_amount=Decimal("500000000"),  # $500M
    )
    mid = _make_company(
        name="MidRaise Inc.",
        slug="midraise-prio-enrich",
        latest_round_amount=Decimal("10000000"),  # $10M
    )
    none = _make_company(
        name="NoRaise Inc.",
        slug="noraise-prio-enrich",
        latest_round_amount=None,  # no funding amount → sorts last
    )
    db.add_all([none, mid, big])  # add in non-priority order on purpose
    await db.flush()
    # Each company has a page long enough to clear the _MIN_TEXT_CHARS bar.
    db.add(_make_raw_page(big.id, url="https://bigraise-enrich.com/"))
    db.add(_make_raw_page(mid.id, url="https://midraise-enrich.com/"))
    db.add(_make_raw_page(none.id, url="https://noraise-enrich.com/"))
    await db.flush()
    await db.commit()

    fake, _calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    # max_companies=2 admits only the two most prominent of the three.
    summary = await run_enrich_companies(db, max_companies=2)

    assert summary.companies_seen == 2
    assert summary.companies_enriched == 2
    await db.refresh(big)
    await db.refresh(mid)
    await db.refresh(none)
    # The two highest-raise companies were enriched; the NULL-amount one was not.
    assert big.description_short == _CANNED_JUDGE.description_short
    assert mid.description_short == _CANNED_JUDGE.description_short
    assert none.description_short is None
    assert none.last_enriched_at is None


async def test_max_companies_funding_round_count_breaks_ties(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When latest_round_amount ties, the company with more funding rounds is
    enriched first within the limited slot."""
    many_rounds = _make_company(
        name="ManyRounds Inc.",
        slug="manyrounds-tie-enrich",
        latest_round_amount=Decimal("10000000"),
        funding_round_count=5,
    )
    few_rounds = _make_company(
        name="FewRounds Inc.",
        slug="fewrounds-tie-enrich",
        latest_round_amount=Decimal("10000000"),  # same amount
        funding_round_count=1,
    )
    db.add_all([few_rounds, many_rounds])
    await db.flush()
    db.add(_make_raw_page(many_rounds.id, url="https://manyrounds-enrich.com/"))
    db.add(_make_raw_page(few_rounds.id, url="https://fewrounds-enrich.com/"))
    await db.flush()
    await db.commit()

    fake, _calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db, max_companies=1)

    assert summary.companies_seen == 1
    assert summary.companies_enriched == 1
    await db.refresh(many_rounds)
    await db.refresh(few_rounds)
    assert many_rounds.description_short == _CANNED_JUDGE.description_short
    assert few_rounds.description_short is None
    assert few_rounds.last_enriched_at is None


async def test_thin_text_company_is_not_selected(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companies whose stored pages are all < 200 chars are excluded in SQL.

    Without the SQL-level exclusion they re-enter the selection every run
    (description_short stays NULL forever) and, sitting at the front of the
    LIMIT N scan, eventually consume the whole per-run budget.
    """
    company = _make_company(slug="enrich-thin")
    db.add(company)
    await db.flush()
    thin_page = RawPage(
        company_id=company.id,
        url="https://thin.com/",
        content="Hi.",  # scrape now stores extracted text; 3 chars
    )
    db.add(thin_page)
    await db.flush()
    await db.commit()

    mock_complete_json = AsyncMock(return_value=_CANNED_JUDGE)
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        mock_complete_json,
    )

    summary = await run_enrich_companies(db)

    mock_complete_json.assert_not_called()
    assert summary.companies_seen == 0
    assert summary.skipped_no_text == 0


async def test_markup_heavy_page_skipped_in_loop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page that is long on bytes but thin on visible text still gets the
    in-loop skip (defends rows that pre-date the extracted-text storage)."""
    company = _make_company(slug="enrich-markup-thin")
    db.add(company)
    await db.flush()
    page = RawPage(
        company_id=company.id,
        url="https://markup.com/",
        # > 200 chars of content, < 200 chars of visible text
        content="<html><head>" + "<meta charset='utf-8'>" * 20 + "</head>"
        "<body><p>Hi.</p></body></html>",
    )
    db.add(page)
    await db.flush()
    await db.commit()

    mock_complete_json = AsyncMock(return_value=_CANNED_JUDGE)
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        mock_complete_json,
    )

    summary = await run_enrich_companies(db)

    mock_complete_json.assert_not_called()
    assert summary.companies_seen == 1
    assert summary.skipped_no_text >= 1


async def test_parked_site_clears_website_and_pages(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company(name="Ninegag", slug="ninegag-parked")
    company.website = "https://ninegag.ai"
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://ninegag.ai/"))
    await db.commit()

    canned = CompanyDescription(
        description_short="The domain ninegag.ai is listed for sale.",
        primary_category="unknown",
        website_state="parked_or_for_sale",
    )
    fake, _calls = _llm_router(judge=canned)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db)
    assert summary.skipped_bad_website == 1
    assert summary.companies_enriched == 0
    # A husk never reaches the describe call — existing behavior stands.
    assert _calls == {"judge": 1, "describe": 0}

    await db.refresh(company)
    assert company.website is None
    assert company.website_resolved_at is None
    assert company.rejected_urls == ["https://ninegag.ai"]
    assert company.description_short is None  # junk prose NOT published
    pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == company.id))
    ).scalars().all()
    assert pages == []  # junk pages dropped so the selection stops re-picking


async def test_not_startup_judgment_excludes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company(name="Old Enterprise", slug="old-enterprise")
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://old.example/"))
    await db.commit()

    canned = CompanyDescription(
        description_short="A 26-year-old customer-service software vendor.",
        primary_category="vertical SaaS",
        website_state="ok",
        is_startup=False,
        not_startup_reason="Founded in 2000; publicly traded.",
        founded_year=2000,
    )
    fake, _calls = _llm_router(judge=canned)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db)
    assert summary.companies_enriched == 1
    assert summary.companies_excluded == 1

    await db.refresh(company)
    assert company.exclusion_reason == "not_a_startup"
    assert company.exclusion_detail == "Founded in 2000; publicly traded."
    assert company.excluded_at is not None
    assert company.eligibility_checked_at is not None
    assert company.year_incorporated == 2000
    # The short description IS stored (audit); exclusion hides it from the
    # catalog. The describe call is skipped for excluded rows — no long
    # description, and no enrichment stamp so a later un-exclusion re-opens
    # the row for --redescribe-outdated.
    assert company.description_short is not None
    assert company.description_long is None
    assert company.enrichment_prompt_version is None
    assert _calls == {"judge": 1, "describe": 0}


async def test_non_us_judgment_excludes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company(name="Bangalore Co", slug="bangalore-co")
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://bangalore.example/"))
    await db.commit()

    canned = CompanyDescription(
        description_short="An Indian HR software company.",
        primary_category="vertical SaaS",
        website_state="ok",
        is_startup=True,
        hq_country="IN",
    )
    fake, _calls = _llm_router(judge=canned)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    await run_enrich_companies(db)
    await db.refresh(company)
    assert company.exclusion_reason == "non_us"
    assert company.hq_country == "IN"


async def test_ok_startup_sets_stamp_without_exclusion(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company(name="Fine Startup", slug="fine-startup")
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://fine.example/"))
    await db.commit()

    canned = CompanyDescription(
        description_short="A developer tools startup.",
        primary_category="developer tools",
        website_state="ok",
        is_startup=None,  # unknown → keep
    )
    fake, _calls = _llm_router(judge=canned)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    await run_enrich_companies(db)
    await db.refresh(company)
    assert company.exclusion_reason is None
    assert company.eligibility_checked_at is not None


# ---------------------------------------------------------------------------
# --backfill-missing-taxonomy tests
# ---------------------------------------------------------------------------

# A canned response that includes an industry label so taxonomy is populated.
_CANNED_WITH_INDUSTRY = CompanyDescription(
    description_short="A fintech company.",
    primary_category="fintech",
    tags=["payments", "smb"],
    website_state="ok",
    industry="fintech",
)


def _make_described_company(
    *,
    name: str,
    slug: str,
    industry_group: str | None = None,
    primary_category: str | None = None,
) -> Company:
    """Company that is fully enriched (has description_long) but may lack taxonomy."""
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        description_short="A short description.",
        description_long="A longer description about this company.",
        primary_category=primary_category,
        industry_group=industry_group,
        last_enriched_at=datetime.now(tz=UTC),
    )


async def test_backfill_selects_null_industry_group(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In backfill mode a described company with NULL industry_group is selected
    and gets industry_group populated from the LLM response."""
    company = _make_described_company(
        name="Backfill Target Inc.",
        slug="backfill-target",
        # NULL industry_group — the case we want to fix
        industry_group=None,
        primary_category="fintech",
    )
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://backfill-target.example/"))
    await db.commit()

    fake, _calls = _llm_router(judge=_CANNED_WITH_INDUSTRY)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db, backfill_missing_taxonomy=True)

    assert summary.companies_seen == 1
    assert summary.companies_enriched == 1
    await db.refresh(company)
    # industry_group must be populated now (null → "fintech" canonical)
    assert company.industry_group == "fintech"


async def test_backfill_selects_null_primary_category(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In backfill mode a described company with NULL primary_category is also
    selected (both columns are checked)."""
    company = _make_described_company(
        name="Backfill Cat Inc.",
        slug="backfill-cat",
        industry_group="fintech",
        primary_category=None,  # missing primary_category
    )
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://backfill-cat.example/"))
    await db.commit()

    fake, _calls = _llm_router(judge=_CANNED_WITH_INDUSTRY)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db, backfill_missing_taxonomy=True)

    assert summary.companies_seen == 1
    assert summary.companies_enriched == 1
    await db.refresh(company)
    assert company.primary_category == "fintech"


async def test_backfill_skips_company_with_both_taxonomy_fields(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In backfill mode a company that already has BOTH industry_group AND
    primary_category is NOT selected — it is already competitor-eligible."""
    company = _make_described_company(
        name="Already Tagged Inc.",
        slug="already-tagged",
        industry_group="fintech",
        primary_category="fintech",
    )
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://already-tagged.example/"))
    await db.commit()

    mock_llm = AsyncMock(return_value=_CANNED_WITH_INDUSTRY)
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        mock_llm,
    )

    summary = await run_enrich_companies(db, backfill_missing_taxonomy=True)

    mock_llm.assert_not_called()
    assert summary.companies_seen == 0


async def test_backfill_does_not_select_description_short_null(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backfill mode requires description_long IS NOT NULL; a company without
    any description at all is NOT selected (normal enrich-companies covers it)."""
    unenriched = _make_company(
        name="Unenriched Co.",
        slug="unenriched-bf",
        description_short=None,
    )
    db.add(unenriched)
    await db.flush()
    db.add(_make_raw_page(unenriched.id, url="https://unenriched-bf.example/"))
    await db.commit()

    mock_llm = AsyncMock(return_value=_CANNED_WITH_INDUSTRY)
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        mock_llm,
    )

    summary = await run_enrich_companies(db, backfill_missing_taxonomy=True)

    mock_llm.assert_not_called()
    assert summary.companies_seen == 0


async def test_backfill_idempotent_second_run(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the first backfill run sets industry_group, a second run is a no-op
    (the company drops out of the backfill selection)."""
    company = _make_described_company(
        name="Idempotent Co.",
        slug="idempotent-bf",
        industry_group=None,
        primary_category="fintech",
    )
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://idempotent-bf.example/"))
    await db.commit()

    fake, _calls = _llm_router(judge=_CANNED_WITH_INDUSTRY)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    # First run: populates industry_group
    summary1 = await run_enrich_companies(db, backfill_missing_taxonomy=True)
    assert summary1.companies_enriched == 1
    await db.refresh(company)
    assert company.industry_group == "fintech"

    # Second run: company now has industry_group set, falls out of selection
    mock_second = AsyncMock(return_value=_CANNED_WITH_INDUSTRY)
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        mock_second,
    )
    summary2 = await run_enrich_companies(db, backfill_missing_taxonomy=True)
    mock_second.assert_not_called()
    assert summary2.companies_seen == 0


async def test_backfill_null_industry_response_leaves_column_null(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the LLM returns no industry label (industry=None), the column stays NULL —
    the company simply remains ineligible rather than receiving a fabricated label."""
    company = _make_described_company(
        name="No Industry Co.",
        slug="no-industry-bf",
        industry_group=None,
        primary_category="fintech",
    )
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://no-industry-bf.example/"))
    await db.commit()

    # LLM returns no industry field
    canned_no_industry = CompanyDescription(
        description_short="A fintech company.",
        primary_category="fintech",
        tags=[],
        website_state="ok",
        industry=None,  # explicitly no industry
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned_no_industry),
    )

    summary = await run_enrich_companies(db, backfill_missing_taxonomy=True)

    # Was enriched (companies_enriched counts the commit), but industry stays NULL
    assert summary.companies_enriched == 1
    await db.refresh(company)
    assert company.industry_group is None  # never fabricate


# ---------------------------------------------------------------------------
# Two-call flow (W-F): describe-call skip conditions + failure isolation
# ---------------------------------------------------------------------------

# Visible text ~340 chars: above _MIN_TEXT_CHARS (200) so the judge runs,
# below _MIN_DESCRIBE_CHARS (700) so the describe call is skipped.
_MEDIUM_PAGE_HTML = (
    "<html><body><p>This page is long enough for the judge but too thin for "
    "a real profile. The company builds a small scheduling tool for local "
    "fitness studios. Clients can book classes online and owners manage the "
    "calendar from a dashboard. A free trial is offered on every plan and "
    "support is answered by email within a day.</p></body></html>"
)


async def test_thin_site_skips_describe_call_but_stamps(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200-700 chars: judged + short-described, but the describe call is
    deliberately skipped and the row is stamped current so
    --redescribe-outdated does not re-select it every run."""
    company = _make_company(name="Thin Included Inc.", slug="thin-included")
    db.add(company)
    await db.flush()
    db.add(RawPage(company_id=company.id, url="https://thin-inc.com/",
                   content=_MEDIUM_PAGE_HTML))
    await db.commit()

    fake, calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db)

    assert calls == {"judge": 1, "describe": 0}
    assert summary.companies_enriched == 1
    assert summary.descriptions_skipped_thin == 1
    assert summary.descriptions_written == 0
    await db.refresh(company)
    assert company.description_short == _CANNED_JUDGE.description_short
    assert company.description_long is None
    assert company.enrichment_prompt_version == DESCRIBE_VERSION
    assert company.eligibility_prompt_version == JUDGE_VERSION


async def test_describe_null_stamps_without_writing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model returning null is a deliberate outcome: no description, but
    the row is stamped current (null-over-fabrication, not an error)."""
    company = _make_company(name="Null Desc Inc.", slug="null-desc")
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://null-desc.com/"))
    await db.commit()

    fake, calls = _llm_router(describe=CompanyLongDescription(description_long=None))
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db)

    assert calls == {"judge": 1, "describe": 1}
    assert summary.descriptions_null == 1
    assert summary.descriptions_written == 0
    await db.refresh(company)
    assert company.description_long is None
    assert company.enrichment_prompt_version == DESCRIBE_VERSION


async def test_describe_error_commits_judge_without_stamp(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A describe-call parse failure must not lose the judge results, and
    must leave the enrichment stamp empty so --redescribe-outdated retries."""
    company = _make_company(name="Desc Fail Inc.", slug="desc-fail")
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://desc-fail.com/"))
    await db.commit()

    fake, calls = _llm_router(describe=LLMParseError("bad JSON"))
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db)

    assert calls == {"judge": 1, "describe": 1}
    assert summary.companies_enriched == 1
    assert summary.description_failures == 1
    await db.refresh(company)
    # Judge results committed...
    assert company.description_short == _CANNED_JUDGE.description_short
    assert company.eligibility_prompt_version == JUDGE_VERSION
    # ...but no long description and no enrichment stamp.
    assert company.description_long is None
    assert company.enrichment_prompt_version is None


async def test_rate_limit_on_describe_commits_judge_and_stops(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 on the SECOND call commits the in-flight company's judge results
    and stops the loop — the next company is never attempted."""
    first = _make_company(
        name="RL Describe 1 Inc.", slug="rl-desc-1",
        latest_round_amount=Decimal("100000000"),  # ordered first
    )
    second = _make_company(name="RL Describe 2 Inc.", slug="rl-desc-2")
    db.add_all([first, second])
    await db.flush()
    db.add(_make_raw_page(first.id, url="https://rl-desc-1.com/"))
    db.add(_make_raw_page(second.id, url="https://rl-desc-2.com/"))
    await db.commit()

    fake, calls = _llm_router(describe=LLMRateLimitError("429"))
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db)

    # One judge + one describe for the first company; loop stopped before
    # the second company's judge call.
    assert calls == {"judge": 1, "describe": 1}
    assert summary.skipped_rate_limited == 1
    assert summary.companies_enriched == 1
    await db.refresh(first)
    await db.refresh(second)
    assert first.description_short == _CANNED_JUDGE.description_short
    assert first.enrichment_prompt_version is None  # redescribe finishes it
    assert second.description_short is None
    assert second.last_enriched_at is None


async def test_backfill_taxonomy_never_calls_describe(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backfill mode is a taxonomy-only repair over already-described rows —
    paying a describe call there would double its cost for nothing."""
    company = _make_described_company(
        name="No Describe Backfill Inc.",
        slug="no-describe-bf",
        industry_group=None,
        primary_category="fintech",
    )
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://no-describe-bf.example/"))
    await db.commit()

    fake, calls = _llm_router(judge=_CANNED_WITH_INDUSTRY)
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_enrich_companies(db, backfill_missing_taxonomy=True)

    assert summary.companies_enriched == 1
    assert calls == {"judge": 1, "describe": 0}
    await db.refresh(company)
    # The pre-existing long description survives untouched.
    assert company.description_long == "A longer description about this company."


# ---------------------------------------------------------------------------
# run_redescribe_outdated (--redescribe-outdated)
# ---------------------------------------------------------------------------


def _make_enriched_company(
    *,
    name: str,
    slug: str,
    enrichment_prompt_version: str | None,
    description_long: str | None = "Old two-line description.",
    exclusion_reason: str | None = None,
    latest_round_amount: Decimal | None = None,
) -> Company:
    """A company that already went through enrich (short description set)."""
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        description_short="A short description.",
        description_long=description_long,
        primary_category="developer tools",
        last_enriched_at=datetime.now(tz=UTC) - timedelta(days=30),
        enrichment_prompt_version=enrichment_prompt_version,
        exclusion_reason=exclusion_reason,
        latest_round_amount=latest_round_amount,
    )


async def test_redescribe_regenerates_only_the_description(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NULL-stamped shown company: description_long is rewritten and stamped;
    judge-owned fields (short description, category, people) are untouched
    and the judge prompt is never called."""
    company = _make_enriched_company(
        name="Redesc Target Inc.", slug="redesc-target",
        enrichment_prompt_version=None,
    )
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://redesc-target.com/"))
    db.add(Person(company_id=company.id, name="Ada Lovelace", role="CEO", rank=1))
    await db.commit()

    fake, calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_redescribe_outdated(db)

    assert calls == {"judge": 0, "describe": 1}
    assert summary.companies_seen == 1
    assert summary.descriptions_written == 1
    await db.refresh(company)
    assert company.description_long == _CANNED_LONG_TEXT
    assert company.enrichment_prompt_version == DESCRIBE_VERSION
    # Judge-owned fields untouched.
    assert company.description_short == "A short description."
    assert company.primary_category == "developer tools"
    people = (
        await db.execute(select(Person).where(Person.company_id == company.id))
    ).scalars().all()
    assert [p.name for p in people] == ["Ada Lovelace"]


async def test_redescribe_selection_boundaries(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current-stamp, excluded, and never-enriched companies are NOT
    selected; outdated-stamp companies are."""
    current = _make_enriched_company(
        name="Current Stamp Inc.", slug="redesc-current",
        enrichment_prompt_version=DESCRIBE_VERSION,
    )
    excluded = _make_enriched_company(
        name="Excluded Inc.", slug="redesc-excluded",
        enrichment_prompt_version=None,
        exclusion_reason="not_a_startup",
    )
    outdated = _make_enriched_company(
        name="Outdated Stamp Inc.", slug="redesc-outdated",
        enrichment_prompt_version="2026-07-10.1",  # pre-split single prompt
    )
    unenriched = _make_company(name="Never Enriched Inc.", slug="redesc-never")
    db.add_all([current, excluded, outdated, unenriched])
    await db.flush()
    for c, url in (
        (current, "https://redesc-current.com/"),
        (excluded, "https://redesc-excluded.com/"),
        (outdated, "https://redesc-outdated.com/"),
        (unenriched, "https://redesc-never.com/"),
    ):
        db.add(_make_raw_page(c.id, url=url))
    await db.commit()

    fake, calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_redescribe_outdated(db)

    assert summary.companies_seen == 1
    assert calls["describe"] == 1
    await db.refresh(outdated)
    assert outdated.description_long == _CANNED_LONG_TEXT
    assert outdated.enrichment_prompt_version == DESCRIBE_VERSION
    for untouched in (current, excluded, unenriched):
        await db.refresh(untouched)
        assert untouched.description_long != _CANNED_LONG_TEXT


async def test_redescribe_orders_null_stamp_first_and_respects_limit(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oldest-version-first: NULL stamps (pre-versioning) sort before dated
    stamps, and --limit bounds the run."""
    dated = _make_enriched_company(
        name="Dated Stamp Inc.", slug="redesc-dated",
        enrichment_prompt_version="2026-07-10.1",
        latest_round_amount=Decimal("900000000"),  # prominence can't save it
    )
    null_stamp = _make_enriched_company(
        name="Null Stamp Inc.", slug="redesc-null",
        enrichment_prompt_version=None,
    )
    db.add_all([dated, null_stamp])
    await db.flush()
    db.add(_make_raw_page(dated.id, url="https://redesc-dated.com/"))
    db.add(_make_raw_page(null_stamp.id, url="https://redesc-null.com/"))
    await db.commit()

    fake, _calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_redescribe_outdated(db, max_companies=1)

    assert summary.companies_seen == 1
    await db.refresh(null_stamp)
    await db.refresh(dated)
    assert null_stamp.description_long == _CANNED_LONG_TEXT
    assert dated.description_long == "Old two-line description."


async def test_redescribe_is_idempotent(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After one run stamps a company current, a second run selects nothing."""
    company = _make_enriched_company(
        name="Idempotent Redesc Inc.", slug="redesc-idem",
        enrichment_prompt_version=None,
    )
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://redesc-idem.com/"))
    await db.commit()

    fake, calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary1 = await run_redescribe_outdated(db)
    assert summary1.descriptions_written == 1

    summary2 = await run_redescribe_outdated(db)
    assert summary2.companies_seen == 0
    assert calls["describe"] == 1  # no second LLM call


async def test_redescribe_null_keeps_existing_description(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the model now says the content is insufficient, the existing
    description survives (never degrade a live page) but the row is stamped
    so it drops out of the selection."""
    company = _make_enriched_company(
        name="Keep Old Desc Inc.", slug="redesc-keep-old",
        enrichment_prompt_version=None,
        description_long="Existing description worth keeping.",
    )
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://redesc-keep-old.com/"))
    await db.commit()

    fake, _calls = _llm_router(
        describe=CompanyLongDescription(description_long=None)
    )
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_redescribe_outdated(db)

    assert summary.descriptions_null == 1
    assert summary.descriptions_written == 0
    await db.refresh(company)
    assert company.description_long == "Existing description worth keeping."
    assert company.enrichment_prompt_version == DESCRIBE_VERSION


async def test_redescribe_thin_text_stamps_without_llm_call(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selected company whose pages fall under the describe bar is stamped
    current without an LLM call (starvation guard), keeping the run cheap."""
    company = _make_enriched_company(
        name="Thin Redesc Inc.", slug="redesc-thin",
        enrichment_prompt_version=None,
        description_long=None,
    )
    db.add(company)
    await db.flush()
    db.add(RawPage(company_id=company.id, url="https://redesc-thin.com/",
                   content=_MEDIUM_PAGE_HTML))
    await db.commit()

    fake, calls = _llm_router()
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_redescribe_outdated(db)

    assert calls == {"judge": 0, "describe": 0}
    assert summary.skipped_thin == 1
    await db.refresh(company)
    assert company.description_long is None
    assert company.enrichment_prompt_version == DESCRIBE_VERSION


async def test_redescribe_rate_limit_stops_loop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _make_enriched_company(
        name="RL Redesc 1 Inc.", slug="rl-redesc-1",
        enrichment_prompt_version=None,
    )
    second = _make_enriched_company(
        name="RL Redesc 2 Inc.", slug="rl-redesc-2",
        enrichment_prompt_version="2026-07-10.1",
    )
    db.add_all([first, second])
    await db.flush()
    db.add(_make_raw_page(first.id, url="https://rl-redesc-1.com/"))
    db.add(_make_raw_page(second.id, url="https://rl-redesc-2.com/"))
    await db.commit()

    fake, calls = _llm_router(describe=LLMRateLimitError("429"))
    monkeypatch.setattr("nous.pipeline.enrich_companies.complete_json", fake)

    summary = await run_redescribe_outdated(db)

    assert calls["describe"] == 1  # stopped after the first 429
    assert summary.skipped_rate_limited == 1
    await db.refresh(first)
    # No stamp on the rate-limited company: a later run retries it.
    assert first.enrichment_prompt_version is None
