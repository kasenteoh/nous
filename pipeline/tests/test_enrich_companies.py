"""Integration tests for the enrich-companies pipeline stage.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via ``alembic upgrade head``.

Tests are skipped when DATABASE_URL is unset or empty.

``complete_json`` is monkeypatched to return canned CompanyDescription objects
so no real LLM calls are made.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Person, RawPage
from nous.llm.client import LLMParseError, LLMRateLimitError
from nous.llm.prompts.company_description import CompanyDescription, PersonExtraction
from nous.pipeline.enrich_companies import run_enrich_companies

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

_CANNED_DESCRIPTION = CompanyDescription(
    description_short="A short description of the company.",
    description_long="A longer description with multiple paragraphs.",
    primary_category="developer tools",
    tags=["open source", "API first", "cloud native"],
    website_state="ok",
)


def _make_company(
    *,
    name: str = "Acme Inc.",
    slug: str = "acme",
    description_short: str | None = None,
    last_enriched_at: datetime | None = None,
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        description_short=description_short,
        last_enriched_at=last_enriched_at,
    )


def _make_raw_page(company_id: Any, *, url: str = "https://acme.com/") -> RawPage:
    return RawPage(
        company_id=company_id,
        url=url,
        content="<html><body><p>This is a substantial enough page to pass the minimum text check. "
        "The company builds developer tools for API-first teams. "
        "Their platform enables engineers to design, test, and deploy APIs at scale. "
        "Founded in 2021, they serve hundreds of enterprise customers globally. "
        "Their flagship product is a cloud-native API gateway with built-in observability. "
        "The team is distributed across North America and Europe.</p></body></html>",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=_CANNED_DESCRIPTION),
    )

    summary = await run_enrich_companies(db)

    await db.refresh(company)
    assert company.description_short == _CANNED_DESCRIPTION.description_short
    assert company.description_long == _CANNED_DESCRIPTION.description_long
    assert company.primary_category == _CANNED_DESCRIPTION.primary_category
    assert company.last_enriched_at is not None
    assert summary.companies_enriched >= 1


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
        description_long="Long.",
        primary_category="developer tools",
        tags=[],
        website_state="ok",
        people=[
            PersonExtraction(name="Ada Lovelace", role="CEO"),
            PersonExtraction(name="Alan Turing", role="CTO"),
        ],
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

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
        description_long="Long.",
        primary_category="fintech",
        tags=["Open Source", "API First", "Cloud Native"],
        website_state="ok",
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

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

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=_CANNED_DESCRIPTION),
    )

    await run_enrich_companies(db)

    await db.refresh(company)
    assert company.last_enriched_payload is not None
    expected_short = _CANNED_DESCRIPTION.description_short
    assert company.last_enriched_payload["description_short"] == expected_short
    expected_cat = _CANNED_DESCRIPTION.primary_category
    assert company.last_enriched_payload["primary_category"] == expected_cat
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

    call_count = 0

    async def _raise_rate_limit(*args: Any, **kwargs: Any) -> CompanyDescription:
        nonlocal call_count
        call_count += 1
        raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        _raise_rate_limit,
    )

    summary = await run_enrich_companies(db)

    # complete_json should have been called exactly once; loop stopped after first error.
    assert call_count == 1
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

    call_count = 0

    async def _fail_first_succeed_second(*args: Any, **kwargs: Any) -> CompanyDescription:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise LLMParseError("parse failed")
        return _CANNED_DESCRIPTION

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        _fail_first_succeed_second,
    )

    summary = await run_enrich_companies(db)

    assert call_count == 2
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

    mock_complete_json = AsyncMock(return_value=_CANNED_DESCRIPTION)
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

    mock_complete_json = AsyncMock(return_value=_CANNED_DESCRIPTION)
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

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=_CANNED_DESCRIPTION),
    )

    summary = await run_enrich_companies(db, refetch_after_days=90)

    assert summary.companies_enriched >= 1
    await db.refresh(company)
    assert company.description_short == _CANNED_DESCRIPTION.description_short


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

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=_CANNED_DESCRIPTION),
    )

    summary = await run_enrich_companies(db, max_companies=1)

    assert summary.companies_seen == 1
    assert summary.companies_enriched == 1


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

    mock_complete_json = AsyncMock(return_value=_CANNED_DESCRIPTION)
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

    mock_complete_json = AsyncMock(return_value=_CANNED_DESCRIPTION)
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
        description_long="Parked page; no product information.",
        primary_category="unknown",
        website_state="parked_or_for_sale",
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_enrich_companies(db)
    assert summary.skipped_bad_website == 1
    assert summary.companies_enriched == 0

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
        description_long="Long text.",
        primary_category="vertical SaaS",
        website_state="ok",
        is_startup=False,
        not_startup_reason="Founded in 2000; publicly traded.",
        founded_year=2000,
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_enrich_companies(db)
    assert summary.companies_enriched == 1
    assert summary.companies_excluded == 1

    await db.refresh(company)
    assert company.exclusion_reason == "not_a_startup"
    assert company.exclusion_detail == "Founded in 2000; publicly traded."
    assert company.excluded_at is not None
    assert company.eligibility_checked_at is not None
    assert company.year_incorporated == 2000
    # Description IS stored (audit), exclusion just hides it from the catalog.
    assert company.description_short is not None


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
        description_long="Long text.",
        primary_category="vertical SaaS",
        website_state="ok",
        is_startup=True,
        hq_country="IN",
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

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
        description_long="Long text.",
        primary_category="developer tools",
        website_state="ok",
        is_startup=None,  # unknown → keep
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    await run_enrich_companies(db)
    await db.refresh(company)
    assert company.exclusion_reason is None
    assert company.eligibility_checked_at is not None
