"""Integration tests for nous.db.upsert.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via ``alembic upgrade head``.

Tests are skipped when DATABASE_URL is unset or empty so they do not fail in
environments without a Postgres instance (e.g., CI without the service container).
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, Person, RawPage
from nous.db.upsert import (
    merge_companies,
    reconcile_funding_round,
    replace_people,
    upsert_raw_page,
)
from nous.llm.prompts.company_description import PersonExtraction
from nous.llm.prompts.funding_extraction import FundingExtraction

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# upsert_raw_page
# ---------------------------------------------------------------------------


def _make_test_company(slug_suffix: str = "upsert") -> Company:
    return Company(
        name=f"TestCo {slug_suffix}",
        slug=f"testco-{slug_suffix}",
        normalized_name=f"testco {slug_suffix}",
        hq_country="US",
        discovered_via="vc_portfolio",
    )


async def test_upsert_raw_page_inserts_new_row(db: AsyncSession) -> None:
    """upsert_raw_page inserts a new RawPage and returns a populated ORM object."""
    company = _make_test_company("new")
    db.add(company)
    await db.flush()

    page = await upsert_raw_page(db, company.id, "https://example.com/", "<html>hello</html>")

    assert page.id is not None
    assert page.company_id == company.id
    assert page.url == "https://example.com/"
    assert page.content == "<html>hello</html>"
    assert page.fetched_at is not None


async def test_upsert_raw_page_updates_existing_row(db: AsyncSession) -> None:
    """upsert_raw_page with same (company_id, url) updates content in-place, leaving one row."""
    company = _make_test_company("update")
    db.add(company)
    await db.flush()

    url = "https://example.com/about"

    # First upsert.
    page1 = await upsert_raw_page(db, company.id, url, "<html>original</html>")
    await db.flush()

    # Second upsert — same key, different content.
    page2 = await upsert_raw_page(db, company.id, url, "<html>updated</html>")
    await db.flush()

    # Same UUID, content changed.
    assert page1.id == page2.id
    assert page2.content == "<html>updated</html>"

    # Only one row in the DB.
    result = await db.execute(
        select(RawPage).where(
            RawPage.company_id == company.id,
            RawPage.url == url,
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].content == "<html>updated</html>"


async def test_upsert_raw_page_different_urls_are_separate_rows(db: AsyncSession) -> None:
    """Different URLs for the same company produce distinct RawPage rows."""
    company = _make_test_company("multi-url")
    db.add(company)
    await db.flush()

    await upsert_raw_page(db, company.id, "https://example.com/", "<html>home</html>")
    await upsert_raw_page(db, company.id, "https://example.com/about", "<html>about</html>")
    await db.flush()

    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# replace_people
# ---------------------------------------------------------------------------


async def _people_for(db: AsyncSession, company_id: object) -> list[Person]:
    result = await db.execute(
        select(Person).where(Person.company_id == company_id).order_by(Person.rank)
    )
    return list(result.scalars().all())


async def test_replace_people_inserts_ranked_rows(db: AsyncSession) -> None:
    company = _make_test_company("people-insert")
    db.add(company)
    await db.flush()

    n = await replace_people(
        db,
        company.id,
        [
            PersonExtraction(name="Ada Lovelace", role="CEO"),
            PersonExtraction(name="Alan Turing", role="CTO"),
        ],
        source_url="https://acme.example/",
    )
    await db.flush()

    assert n == 2
    rows = await _people_for(db, company.id)
    assert [(r.name, r.role, r.rank) for r in rows] == [
        ("Ada Lovelace", "CEO", 1),
        ("Alan Turing", "CTO", 2),
    ]
    assert all(r.source_url == "https://acme.example/" for r in rows)


async def test_replace_people_is_idempotent(db: AsyncSession) -> None:
    company = _make_test_company("people-idem")
    db.add(company)
    await db.flush()

    people = [PersonExtraction(name="Grace Hopper", role="Founder")]
    await replace_people(db, company.id, people, source_url=None)
    await db.flush()
    await replace_people(db, company.id, people, source_url=None)
    await db.flush()

    rows = await _people_for(db, company.id)
    assert len(rows) == 1
    assert rows[0].name == "Grace Hopper"


async def test_replace_people_dedups_case_insensitive(db: AsyncSession) -> None:
    company = _make_test_company("people-dedup")
    db.add(company)
    await db.flush()

    n = await replace_people(
        db,
        company.id,
        [
            PersonExtraction(name="Ada Lovelace", role="CEO"),
            PersonExtraction(name="ada lovelace", role="Co-founder"),
        ],
        source_url=None,
    )
    await db.flush()

    assert n == 1  # second is a case-insensitive duplicate
    rows = await _people_for(db, company.id)
    assert len(rows) == 1
    assert rows[0].name == "Ada Lovelace"  # first-seen casing wins


async def test_replace_people_empty_clears(db: AsyncSession) -> None:
    company = _make_test_company("people-clear")
    db.add(company)
    await db.flush()

    await replace_people(
        db, company.id, [PersonExtraction(name="X", role="CEO")], source_url=None
    )
    await db.flush()

    n = await replace_people(db, company.id, [], source_url=None)
    await db.flush()

    assert n == 0
    rows = await _people_for(db, company.id)
    assert rows == []


# ---------------------------------------------------------------------------
# funding_round_count maintenance
# ---------------------------------------------------------------------------


def _make_quality_company(name: str, slug: str) -> Company:
    return Company(
        name=name, slug=slug, normalized_name=slug.replace("-", " "), hq_country="US"
    )


async def test_reconcile_funding_round_maintains_count(db: AsyncSession) -> None:
    company = _make_quality_company("Counted Co", "counted-co")
    db.add(company)
    await db.flush()

    extraction = FundingExtraction(
        is_funding_announcement=True,
        round_type="Seed",
        amount_raised_usd=1_000_000,
        announced_date=date(2026, 5, 1),
        confidence="high",
    )
    _, created = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=extraction,
        primary_news_url="https://example.com/a",
    )
    assert created is True
    await db.refresh(company)
    assert company.funding_round_count == 1

    # Re-running the same extraction merges (created=False) and count stays 1.
    _, created = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=extraction,
        primary_news_url="https://example.com/b",
    )
    assert created is False
    await db.refresh(company)
    assert company.funding_round_count == 1


# ---------------------------------------------------------------------------
# reconcile_funding_round — None+None guard (Task 4.2 regression tests)
# ---------------------------------------------------------------------------


async def test_reconcile_null_null_yields_two_rows(db: AsyncSession) -> None:
    """Two vague extractions (round_type=None, announced_date=None) for the
    same company must each produce their OWN row rather than collapsing into
    one.  The None+None guard in reconcile_funding_round forces an INSERT
    whenever both discriminators are absent.
    """
    company = _make_quality_company("Vague Co", "vague-co")
    db.add(company)
    await db.flush()

    null_null = FundingExtraction(
        is_funding_announcement=True,
        round_type=None,
        announced_date=None,
        confidence="low",
    )

    _, created1 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=null_null,
        primary_news_url="https://example.com/headline-1",
    )
    assert created1 is True

    _, created2 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=null_null,
        primary_news_url="https://example.com/headline-2",
    )
    assert created2 is True  # must NOT merge with the first row

    result = await db.execute(
        select(FundingRound).where(FundingRound.company_id == company.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 2, (
        "expected 2 separate rounds for two vague headlines; got "
        f"{len(rows)} (None+None guard not firing)"
    )

    await db.refresh(company)
    assert company.funding_round_count == 2


async def test_reconcile_matching_type_and_date_still_merges(db: AsyncSession) -> None:
    """A pair of extractions that share a non-None round_type AND a date within
    the proximity window must still merge into a single row (normal path
    unaffected by the None+None guard).
    """
    company = _make_quality_company("Typed Co", "typed-co")
    db.add(company)
    await db.flush()

    first = FundingExtraction(
        is_funding_announcement=True,
        round_type="Series A",
        announced_date=date(2026, 3, 1),
        confidence="medium",
    )
    second = FundingExtraction(
        is_funding_announcement=True,
        round_type="Series A",
        announced_date=date(2026, 3, 15),  # within 60-day window
        amount_raised_usd=5_000_000,
        confidence="high",
    )

    _, created1 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=first,
        primary_news_url="https://example.com/series-a-1",
    )
    assert created1 is True

    row2, created2 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=second,
        primary_news_url="https://example.com/series-a-2",
    )
    assert created2 is False  # matched and merged
    assert row2.amount_raised == 5_000_000  # null-fill from second extraction

    result = await db.execute(
        select(FundingRound).where(FundingRound.company_id == company.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1, "matching type+date pair must reconcile to a single row"

    await db.refresh(company)
    assert company.funding_round_count == 1


# ---------------------------------------------------------------------------
# reconcile_funding_round — amount-based merging (duplicate-rounds fix)
# ---------------------------------------------------------------------------


async def test_reconcile_amount_match_merges_helion_case(db: AsyncSession) -> None:
    """The Helion case: a company-site extraction with round_type + date, then a
    Google-News extraction with the SAME amount but null round_type AND null
    date, must MERGE into one row (equal amount is a same-round signal). The
    survivor keeps the informative round_type + date.
    """
    company = _make_quality_company("Helion Energy", "helion-energy")
    db.add(company)
    await db.flush()

    typed_dated = FundingExtraction(
        is_funding_announcement=True,
        round_type="Series G",
        amount_raised_usd=465_000_000,
        announced_date=date(2025, 1, 20),
        confidence="high",
    )
    null_null_same_amount = FundingExtraction(
        is_funding_announcement=True,
        round_type=None,
        announced_date=None,
        amount_raised_usd=465_000_000,
        confidence="low",
    )

    _, created1 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=typed_dated,
        primary_news_url="https://helion.com/series-g",
    )
    assert created1 is True

    row2, created2 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=null_null_same_amount,
        primary_news_url="https://news.google.com/articles/abc",
    )
    assert created2 is False, "same-amount null/null extraction must merge, not insert"

    result = await db.execute(
        select(FundingRound).where(FundingRound.company_id == company.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1, (
        "Helion case: 1 typed+dated round and a same-amount null/null round "
        f"must collapse to ONE row; got {len(rows)}"
    )
    survivor = rows[0]
    assert survivor.round_type == "Series G"  # informative type retained
    assert survivor.announced_date == date(2025, 1, 20)  # informative date retained
    assert survivor.amount_raised == 465_000_000
    # First-write-wins on the attribution URL.
    assert survivor.primary_news_url == "https://helion.com/series-g"

    await db.refresh(company)
    assert company.funding_round_count == 1


async def test_reconcile_amount_match_merges_when_first_was_null_null(
    db: AsyncSession,
) -> None:
    """Order-independence: a null/null extraction lands first, then a typed+dated
    extraction with the SAME amount arrives. They must still merge to one row,
    with the typed/dated values upgrading the survivor.
    """
    company = _make_quality_company("Orderless Co", "orderless-co")
    db.add(company)
    await db.flush()

    null_null_first = FundingExtraction(
        is_funding_announcement=True,
        round_type=None,
        announced_date=None,
        amount_raised_usd=100_000_000,
        confidence="low",
    )
    typed_dated_second = FundingExtraction(
        is_funding_announcement=True,
        round_type="Series C",
        announced_date=date(2026, 2, 1),
        amount_raised_usd=100_000_000,
        confidence="high",
    )

    _, created1 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=null_null_first,
        primary_news_url="https://news.google.com/articles/xyz",
    )
    assert created1 is True

    row2, created2 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=typed_dated_second,
        primary_news_url="https://techcrunch.com/series-c",
    )
    assert created2 is False  # equal amount + compatible (null) type → merge

    result = await db.execute(
        select(FundingRound).where(FundingRound.company_id == company.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    survivor = rows[0]
    # The null-typed survivor is upgraded with the later round_type + date.
    assert survivor.round_type == "Series C"
    assert survivor.announced_date == date(2026, 2, 1)
    assert survivor.extraction_confidence == "high"  # upgraded, never downgraded
    # First-write-wins: the original (null/null) attribution URL stays.
    assert survivor.primary_news_url == "https://news.google.com/articles/xyz"


async def test_reconcile_distinct_amounts_stay_separate(db: AsyncSession) -> None:
    """Two extractions with DIFFERENT amounts (both null type/date) must remain
    two rows — amount equality is the merge signal, so distinct amounts don't
    merge.
    """
    company = _make_quality_company("Two Rounds Co", "two-rounds-co")
    db.add(company)
    await db.flush()

    first = FundingExtraction(
        is_funding_announcement=True,
        round_type=None,
        announced_date=None,
        amount_raised_usd=10_000_000,
        confidence="medium",
    )
    second = FundingExtraction(
        is_funding_announcement=True,
        round_type=None,
        announced_date=None,
        amount_raised_usd=50_000_000,  # different amount
        confidence="medium",
    )

    _, created1 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=first,
        primary_news_url="https://example.com/round-1",
    )
    _, created2 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=second,
        primary_news_url="https://example.com/round-2",
    )
    assert created1 is True
    assert created2 is True, "distinct amounts must NOT merge"

    result = await db.execute(
        select(FundingRound).where(FundingRound.company_id == company.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 2

    await db.refresh(company)
    assert company.funding_round_count == 2


async def test_reconcile_same_amount_contradicting_types_stay_separate(
    db: AsyncSession,
) -> None:
    """Two extractions with the SAME amount but DIFFERENT non-null round_types
    ("Seed" vs "Series C") are different rounds and must NOT merge, even though
    the amounts coincide.
    """
    company = _make_quality_company("Coincident Co", "coincident-co")
    db.add(company)
    await db.flush()

    seed = FundingExtraction(
        is_funding_announcement=True,
        round_type="Seed",
        amount_raised_usd=20_000_000,
        announced_date=date(2024, 1, 1),
        confidence="high",
    )
    series_c = FundingExtraction(
        is_funding_announcement=True,
        round_type="Series C",
        amount_raised_usd=20_000_000,  # same amount, contradicting type
        announced_date=date(2026, 1, 1),
        confidence="high",
    )

    _, created1 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=seed,
        primary_news_url="https://example.com/seed",
    )
    _, created2 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=series_c,
        primary_news_url="https://example.com/series-c",
    )
    assert created1 is True
    assert created2 is True, (
        "same amount but contradicting non-null round_types must stay separate"
    )

    result = await db.execute(
        select(FundingRound).where(FundingRound.company_id == company.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 2

    await db.refresh(company)
    assert company.funding_round_count == 2


async def test_merge_companies_refreshes_survivor_count(db: AsyncSession) -> None:
    survivor = _make_quality_company("Survivor Co", "survivor-co")
    loser = _make_quality_company("Loser Co", "loser-co")
    db.add_all([survivor, loser])
    await db.flush()
    # Seed BOTH sides so the assertion proves the count is recomputed from the
    # table (1 own + 1 inherited = 2), not transferred from the loser.
    db.add_all(
        [
            FundingRound(
                company_id=survivor.id,
                round_type="Series A",
                announced_date=date(2025, 6, 1),
            ),
            FundingRound(
                company_id=loser.id,
                round_type="Seed",
                announced_date=date(2026, 1, 1),
            ),
        ]
    )
    await db.flush()

    await merge_companies(db, survivor_id=survivor.id, loser_id=loser.id)
    await db.refresh(survivor)
    assert survivor.funding_round_count == 2


async def test_reconcile_placeholder_type_merges_with_real_round(
    db: AsyncSession,
) -> None:
    """A 'Series ?' extraction at the same amount merges into the stored
    Series F round instead of spawning a fake-typed sibling (sambanova,
    2026-07-16 QA). The stored real type is never overwritten."""
    company = _make_quality_company("PlaceholderCo", "placeholderco")
    db.add(company)
    await db.flush()

    real = FundingExtraction(
        is_funding_announcement=True,
        round_type="Series F",
        amount_raised_usd=1_000_000_000,
        announced_date=date(2026, 7, 8),
        confidence="high",
    )
    _, created = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=real,
        primary_news_url="https://siliconangle.com/real",
    )
    assert created is True

    placeholder = FundingExtraction(
        is_funding_announcement=True,
        round_type="Series ?",
        amount_raised_usd=1_000_000_000,
        announced_date=None,
        confidence="low",
    )
    row, created = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=placeholder,
        primary_news_url="https://aggregator.example.com/mislabel",
    )
    assert created is False
    assert row.round_type == "Series F"


async def test_reconcile_placeholder_type_never_persists(db: AsyncSession) -> None:
    """A brand-new extraction with a placeholder type stores round_type=None."""
    company = _make_quality_company("FreshPlaceholderCo", "freshplaceholderco")
    db.add(company)
    await db.flush()
    extraction = FundingExtraction(
        is_funding_announcement=True,
        round_type="Series ?",
        amount_raised_usd=50_000_000,
        announced_date=date(2026, 6, 1),
        confidence="medium",
    )
    row, created = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=extraction,
        primary_news_url="https://example.com/fresh",
    )
    assert created is True
    assert row.round_type is None
