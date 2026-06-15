"""Integration tests for the repair-duplicate-rounds cleanup stage.

Covers:
- The Helion case: a typed+dated round plus several same-amount null/null rows
  collapse to ONE survivor, the survivor keeping the informative fields.
- Fully-empty junk rows (type, date, amount all null) are deleted.
- Distinct amounts and contradicting non-null round_types are NOT collapsed.
- funding_round_investors are repointed onto the survivor (unique-pair safe,
  is_lead promoted).
- Idempotency: a second run collapses/deletes nothing.

Requires DATABASE_URL (skipped otherwise).
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    FundingRound,
    FundingRoundInvestor,
    Investor,
)
from nous.pipeline.repair_duplicate_rounds import run_repair_duplicate_rounds

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _co(name: str, slug: str) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
    )


async def _rounds_for(db: AsyncSession, company_id: object) -> list[FundingRound]:
    result = await db.execute(
        select(FundingRound).where(FundingRound.company_id == company_id)
    )
    return list(result.scalars().all())


async def test_collapses_helion_duplicate_set(db: AsyncSession) -> None:
    """1 typed+dated row + 4 same-amount null/null rows → ONE survivor that
    keeps the round_type + date, and funding_round_count is corrected.
    """
    co = _co("Helion Energy", "helion-energy-cleanup")
    db.add(co)
    await db.flush()

    # The "good" row: from the company site, has type + date.
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series G",
            amount_raised=465_000_000,
            announced_date=date(2025, 1, 20),
            primary_news_url="https://helion.com/series-g",
            extraction_confidence="high",
        )
    )
    # Four Google-News dupes: same amount, null type, null date.
    for i in range(4):
        db.add(
            FundingRound(
                company_id=co.id,
                round_type=None,
                amount_raised=465_000_000,
                announced_date=None,
                primary_news_url=f"https://news.google.com/articles/{i}",
                extraction_confidence="low",
            )
        )
    # Stale denormalized count (as prod would have it).
    co.funding_round_count = 5
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)

    assert summary.duplicate_rows_merged == 4
    assert summary.empty_rows_deleted == 0
    assert summary.companies_repaired == 1

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1, "the 5 same-amount rows must collapse to one survivor"
    survivor = rows[0]
    assert survivor.round_type == "Series G"
    assert survivor.announced_date == date(2025, 1, 20)
    assert survivor.amount_raised == 465_000_000

    await db.refresh(co)
    assert co.funding_round_count == 1


async def test_deletes_fully_empty_rows(db: AsyncSession) -> None:
    """Rows with round_type, announced_date and amount_raised all null are junk
    and get deleted; a real round on the same company survives.
    """
    co = _co("Empties Co", "empties-co")
    db.add(co)
    await db.flush()

    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Seed",
            amount_raised=3_000_000,
            announced_date=date(2026, 1, 1),
        )
    )
    for _ in range(3):
        db.add(
            FundingRound(
                company_id=co.id,
                round_type=None,
                amount_raised=None,
                announced_date=None,
            )
        )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)

    assert summary.empty_rows_deleted == 3
    assert summary.duplicate_rows_merged == 0

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1
    assert rows[0].round_type == "Seed"

    await db.refresh(co)
    assert co.funding_round_count == 1


async def test_distinct_amounts_and_types_preserved(db: AsyncSession) -> None:
    """Two genuinely different rounds — different amounts, OR same amount with
    contradicting non-null types — must NOT be collapsed.
    """
    co = _co("Multi Round Co", "multi-round-co")
    db.add(co)
    await db.flush()

    # Different amounts.
    db.add(
        FundingRound(
            company_id=co.id, round_type="Seed", amount_raised=5_000_000
        )
    )
    db.add(
        FundingRound(
            company_id=co.id, round_type="Series A", amount_raised=25_000_000
        )
    )
    # Same amount, contradicting non-null types.
    db.add(
        FundingRound(
            company_id=co.id, round_type="Series B", amount_raised=80_000_000
        )
    )
    db.add(
        FundingRound(
            company_id=co.id, round_type="Series C", amount_raised=80_000_000
        )
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)

    assert summary.duplicate_rows_merged == 0
    assert summary.empty_rows_deleted == 0
    assert summary.companies_repaired == 0

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 4, "distinct rounds must all survive"


async def test_repoints_investor_links_onto_survivor(db: AsyncSession) -> None:
    """When duplicate rounds carry funding_round_investors, the links move to the
    survivor, are de-duplicated on the unique (round, investor) pair, and is_lead
    is promoted (sticky).
    """
    co = _co("Linked Co", "linked-co")
    inv_a = Investor(
        name="Lead Capital", name_normalized="lead capital", slug="lead-capital-rdr"
    )
    inv_b = Investor(
        name="Follow Ventures",
        name_normalized="follow ventures",
        slug="follow-ventures-rdr",
    )
    db.add_all([co, inv_a, inv_b])
    await db.flush()

    # Survivor candidate: typed + dated (so it wins survivor selection).
    survivor = FundingRound(
        company_id=co.id,
        round_type="Series A",
        amount_raised=40_000_000,
        announced_date=date(2026, 3, 1),
        primary_news_url="https://techcrunch.com/series-a",
        extraction_confidence="high",
    )
    # Duplicate: same amount, null type/date.
    dup = FundingRound(
        company_id=co.id,
        round_type=None,
        amount_raised=40_000_000,
        announced_date=None,
        primary_news_url="https://news.google.com/articles/1",
        extraction_confidence="low",
    )
    db.add_all([survivor, dup])
    await db.flush()

    # inv_a links to BOTH rounds; on the survivor as a participant, on the dup
    # as lead — the merge must promote the survivor's link to lead.
    db.add_all(
        [
            FundingRoundInvestor(
                funding_round_id=survivor.id, investor_id=inv_a.id, is_lead=False
            ),
            FundingRoundInvestor(
                funding_round_id=dup.id, investor_id=inv_a.id, is_lead=True
            ),
            # inv_b only links to the dup — it must repoint to the survivor.
            FundingRoundInvestor(
                funding_round_id=dup.id, investor_id=inv_b.id, is_lead=False
            ),
        ]
    )
    survivor_id = survivor.id
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)
    assert summary.duplicate_rows_merged == 1

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1
    assert rows[0].id == survivor_id

    links = (
        (
            await db.execute(
                select(FundingRoundInvestor).where(
                    FundingRoundInvestor.funding_round_id == survivor_id
                )
            )
        )
        .scalars()
        .all()
    )
    # Exactly two links survive (inv_a deduped to one, inv_b repointed).
    by_investor = {link.investor_id: link for link in links}
    assert set(by_investor) == {inv_a.id, inv_b.id}
    assert by_investor[inv_a.id].is_lead is True, "is_lead must be promoted (sticky)"
    assert by_investor[inv_b.id].is_lead is False


async def test_idempotent_second_run_is_noop(db: AsyncSession) -> None:
    """A second run over already-repaired data collapses/deletes nothing."""
    co = _co("Idem Co", "idem-co")
    db.add(co)
    await db.flush()
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series G",
            amount_raised=465_000_000,
            announced_date=date(2025, 1, 20),
        )
    )
    for i in range(3):
        db.add(
            FundingRound(
                company_id=co.id,
                round_type=None,
                amount_raised=465_000_000,
                announced_date=None,
                primary_news_url=f"https://news.google.com/articles/{i}",
            )
        )
    # One fully-empty junk row too.
    db.add(FundingRound(company_id=co.id))
    await db.commit()

    first = await run_repair_duplicate_rounds(db)
    assert first.duplicate_rows_merged == 3
    assert first.empty_rows_deleted == 1

    second = await run_repair_duplicate_rounds(db)
    assert second.duplicate_rows_merged == 0
    assert second.empty_rows_deleted == 0
    assert second.companies_repaired == 0

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1

    await db.refresh(co)
    assert co.funding_round_count == 1


async def test_valuation_only_row_is_not_deleted(db: AsyncSession) -> None:
    """A row with valuation_post_money set but round_type/announced_date/
    amount_raised all NULL carries a real sourced fact and must NOT be deleted,
    and must NOT be counted in empty_rows_deleted.
    """
    co = _co("Val Only Co", "val-only-co")
    db.add(co)
    await db.flush()

    # Valuation-only row: "Company X valued at $2B" — no round type, no date,
    # no amount, but a real post-money valuation sourced from an article.
    db.add(
        FundingRound(
            company_id=co.id,
            round_type=None,
            amount_raised=None,
            announced_date=None,
            valuation_post_money=2_000_000_000,
            valuation_source="https://techcrunch.com/val-article",
        )
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)

    assert summary.empty_rows_deleted == 0, "valuation-only row must not be deleted"
    assert summary.companies_repaired == 0

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1, "the valuation-only row must survive"
    assert rows[0].valuation_post_money == 2_000_000_000


async def test_valuation_source_only_row_is_not_deleted(db: AsyncSession) -> None:
    """A row with valuation_source set but everything else NULL must survive.
    Even without a numeric valuation, the source URL is a real attribution.
    """
    co = _co("Val Source Only Co", "val-source-only-co")
    db.add(co)
    await db.flush()

    db.add(
        FundingRound(
            company_id=co.id,
            round_type=None,
            amount_raised=None,
            announced_date=None,
            valuation_post_money=None,
            valuation_source="https://bloomberg.com/val-mention",
        )
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)

    assert summary.empty_rows_deleted == 0, "valuation-source-only row must not be deleted"
    assert summary.companies_repaired == 0

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1, "the valuation-source-only row must survive"
    assert rows[0].valuation_source == "https://bloomberg.com/val-mention"


async def test_truly_fully_empty_row_is_still_deleted(db: AsyncSession) -> None:
    """A row with round_type/announced_date/amount_raised/valuation_post_money/
    valuation_source all NULL is true junk and must still be deleted (regression
    guard — existing behaviour preserved).
    """
    co = _co("All Null Co", "all-null-co")
    db.add(co)
    await db.flush()

    # One real round plus two truly-empty junk rows.
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series A",
            amount_raised=10_000_000,
            announced_date=date(2026, 1, 1),
        )
    )
    for _ in range(2):
        db.add(
            FundingRound(
                company_id=co.id,
                round_type=None,
                amount_raised=None,
                announced_date=None,
                valuation_post_money=None,
                valuation_source=None,
            )
        )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)

    assert summary.empty_rows_deleted == 2, "two truly-empty rows must be deleted"
    assert summary.companies_repaired == 1

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1
    assert rows[0].round_type == "Series A"


async def test_dry_run_writes_nothing(db: AsyncSession) -> None:
    """--dry-run reports the would-be collapse but leaves all rows in place."""
    co = _co("Dry Co", "dry-co")
    db.add(co)
    await db.flush()
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series A",
            amount_raised=10_000_000,
            announced_date=date(2026, 1, 1),
        )
    )
    db.add(
        FundingRound(
            company_id=co.id, round_type=None, amount_raised=10_000_000
        )
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db, dry_run=True)
    assert summary.dry_run is True
    assert summary.duplicate_rows_merged == 1

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 2, "dry-run must not delete or merge anything"
