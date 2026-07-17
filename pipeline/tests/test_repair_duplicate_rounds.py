"""Integration tests for the repair-duplicate-rounds cleanup stage.

Covers:
- The Helion case: a typed+dated round plus several same-amount null/null rows
  collapse to ONE survivor, the survivor keeping the informative fields.
- Fully-empty junk rows (type, date, amount all null) are deleted.
- Distinct amounts and contradicting non-null round_types are NOT collapsed.
- funding_round_investors are repointed onto the survivor (unique-pair safe,
  is_lead promoted).
- Idempotency: a second run collapses/deletes nothing.
- The Perplexity case: valuation-only phantom rows (no amount/type/date, only a
  valuation_post_money) collapse into a "more complete" sibling carrying the
  SAME valuation — preserving the valuation, repointing investors — while a
  lone phantom whose valuation matches no sibling is preserved.

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
    NewsArticle,
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


# ── Phantom valuation-only rows (the Perplexity case) ──────────────────────


async def test_collapses_perplexity_phantom_valuation_rows(db: AsyncSession) -> None:
    """The Perplexity shape: TWO valuation-only phantom rows (no amount, no type,
    no date — only a $20B post-money) alongside a real $20B round collapse to the
    one real round, with the $20B valuation intact (the #107 invariant holds).
    """
    co = _co("Perplexity", "perplexity-cleanup")
    db.add(co)
    await db.flush()

    val = 20_000_000_000

    # The real round: has an amount + type + date AND the same $20B valuation.
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series D",
            amount_raised=500_000_000,
            announced_date=date(2025, 12, 1),
            valuation_post_money=val,
            valuation_source="https://techcrunch.com/perplexity-series-d",
            primary_news_url="https://techcrunch.com/perplexity-series-d",
            extraction_confidence="high",
        )
    )
    # Two phantom shells: nothing but the $20B post-money valuation.
    for _ in range(2):
        db.add(
            FundingRound(
                company_id=co.id,
                round_type=None,
                amount_raised=None,
                announced_date=None,
                valuation_post_money=val,
                valuation_source=None,
                primary_news_url="https://news.google.com/articles/phantom",
                extraction_confidence="low",
            )
        )
    co.funding_round_count = 3
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)

    assert summary.phantom_valuation_rows_merged == 2
    assert summary.empty_rows_deleted == 0, "phantom rows carry a valuation — not empty"
    assert summary.duplicate_rows_merged == 0, "no same-amount dupes here"
    assert summary.companies_repaired == 1

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1, "two phantoms collapse into the one real round"
    survivor = rows[0]
    assert survivor.round_type == "Series D"
    assert survivor.amount_raised == 500_000_000
    assert survivor.announced_date == date(2025, 12, 1)
    # The #107 invariant: the valuation survives on the sibling.
    assert survivor.valuation_post_money == val

    await db.refresh(co)
    assert co.funding_round_count == 1


async def test_phantom_valuation_with_no_matching_sibling_is_preserved(
    db: AsyncSession,
) -> None:
    """A lone valuation-only row whose valuation matches NO sibling must be kept
    — it may be the only carrier of that valuation. PR #107 invariant.
    """
    co = _co("Lone Val Co", "lone-val-co")
    db.add(co)
    await db.flush()

    # A real round carrying a DIFFERENT valuation.
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series A",
            amount_raised=30_000_000,
            announced_date=date(2026, 2, 1),
            valuation_post_money=300_000_000,
        )
    )
    # A valuation-only phantom whose $9B matches nothing else on the company.
    db.add(
        FundingRound(
            company_id=co.id,
            round_type=None,
            amount_raised=None,
            announced_date=None,
            valuation_post_money=9_000_000_000,
            valuation_source="https://bloomberg.com/9b-mention",
        )
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)

    assert summary.phantom_valuation_rows_merged == 0, "no matching sibling — keep it"
    assert summary.empty_rows_deleted == 0
    assert summary.companies_repaired == 0

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 2, "both the real round and the lone phantom survive"
    vals = {r.valuation_post_money for r in rows}
    assert vals == {300_000_000, 9_000_000_000}


async def test_phantom_valuation_only_siblings_are_preserved(
    db: AsyncSession,
) -> None:
    """Two valuation-only rows sharing a valuation but with NO 'more complete'
    sibling (neither has an amount/type/date) must NOT collapse — neither is a
    valid merge target, so both are kept. (Pass 2's amount path owns nothing
    here, and we never pick a phantom as the survivor for the valuation merge.)
    """
    co = _co("Two Phantoms Co", "two-phantoms-co")
    db.add(co)
    await db.flush()

    val = 5_000_000_000
    for _ in range(2):
        db.add(
            FundingRound(
                company_id=co.id,
                round_type=None,
                amount_raised=None,
                announced_date=None,
                valuation_post_money=val,
                valuation_source="https://example.com/val",
            )
        )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)

    assert summary.phantom_valuation_rows_merged == 0
    assert summary.empty_rows_deleted == 0
    assert summary.companies_repaired == 0

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 2, "no complete sibling to merge into — keep both phantoms"


async def test_phantom_valuation_repoints_investor_links(db: AsyncSession) -> None:
    """A phantom valuation row's funding_round_investors move to the sibling,
    de-duplicated on the unique (round, investor) pair with is_lead promoted —
    links are repointed, never lost.
    """
    co = _co("Phantom Linked Co", "phantom-linked-co")
    inv_a = Investor(
        name="Lead Capital",
        name_normalized="lead capital phantom",
        slug="lead-capital-phantom",
    )
    inv_b = Investor(
        name="Follow Ventures",
        name_normalized="follow ventures phantom",
        slug="follow-ventures-phantom",
    )
    db.add_all([co, inv_a, inv_b])
    await db.flush()

    val = 20_000_000_000
    # The "more complete" sibling — has an amount + type + the same valuation.
    sibling = FundingRound(
        company_id=co.id,
        round_type="Series D",
        amount_raised=500_000_000,
        announced_date=date(2025, 12, 1),
        valuation_post_money=val,
        primary_news_url="https://techcrunch.com/series-d",
        extraction_confidence="high",
    )
    # The phantom — only the $20B valuation, but it carries investor links.
    phantom = FundingRound(
        company_id=co.id,
        round_type=None,
        amount_raised=None,
        announced_date=None,
        valuation_post_money=val,
        primary_news_url="https://news.google.com/articles/phantom",
        extraction_confidence="low",
    )
    db.add_all([sibling, phantom])
    await db.flush()

    # inv_a links to BOTH; on the sibling as participant, on the phantom as lead
    # — the merge must promote the sibling's link to lead. inv_b only on the
    # phantom — it must repoint to the sibling.
    db.add_all(
        [
            FundingRoundInvestor(
                funding_round_id=sibling.id, investor_id=inv_a.id, is_lead=False
            ),
            FundingRoundInvestor(
                funding_round_id=phantom.id, investor_id=inv_a.id, is_lead=True
            ),
            FundingRoundInvestor(
                funding_round_id=phantom.id, investor_id=inv_b.id, is_lead=False
            ),
        ]
    )
    sibling_id = sibling.id
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)
    assert summary.phantom_valuation_rows_merged == 1

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1
    assert rows[0].id == sibling_id
    assert rows[0].valuation_post_money == val

    links = (
        (
            await db.execute(
                select(FundingRoundInvestor).where(
                    FundingRoundInvestor.funding_round_id == sibling_id
                )
            )
        )
        .scalars()
        .all()
    )
    by_investor = {link.investor_id: link for link in links}
    assert set(by_investor) == {inv_a.id, inv_b.id}, "no investor link lost"
    assert by_investor[inv_a.id].is_lead is True, "is_lead promoted (sticky)"
    assert by_investor[inv_b.id].is_lead is False


async def test_phantom_valuation_collapse_is_idempotent(db: AsyncSession) -> None:
    """A second run over already-collapsed phantom data does nothing."""
    co = _co("Phantom Idem Co", "phantom-idem-co")
    db.add(co)
    await db.flush()

    val = 20_000_000_000
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series D",
            amount_raised=500_000_000,
            announced_date=date(2025, 12, 1),
            valuation_post_money=val,
        )
    )
    for _ in range(2):
        db.add(
            FundingRound(
                company_id=co.id,
                valuation_post_money=val,
                primary_news_url="https://news.google.com/articles/phantom",
            )
        )
    await db.commit()

    first = await run_repair_duplicate_rounds(db)
    assert first.phantom_valuation_rows_merged == 2

    second = await run_repair_duplicate_rounds(db)
    assert second.phantom_valuation_rows_merged == 0
    assert second.empty_rows_deleted == 0
    assert second.duplicate_rows_merged == 0
    assert second.companies_repaired == 0

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1
    assert rows[0].valuation_post_money == val


async def test_phantom_valuation_dry_run_writes_nothing(db: AsyncSession) -> None:
    """--dry-run reports the would-be phantom collapse but leaves rows in place."""
    co = _co("Phantom Dry Co", "phantom-dry-co")
    db.add(co)
    await db.flush()

    val = 20_000_000_000
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series D",
            amount_raised=500_000_000,
            announced_date=date(2025, 12, 1),
            valuation_post_money=val,
        )
    )
    db.add(
        FundingRound(
            company_id=co.id,
            valuation_post_money=val,
            primary_news_url="https://news.google.com/articles/phantom",
        )
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db, dry_run=True)
    assert summary.dry_run is True
    assert summary.phantom_valuation_rows_merged == 1

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 2, "dry-run must not merge or delete anything"


async def test_phantom_valuation_prefers_more_complete_sibling(
    db: AsyncSession,
) -> None:
    """When several rounds share the phantom's valuation, the phantom merges into
    a 'more complete' one (has amount/type/date), never into another bare row.
    """
    co = _co("Pick Sibling Co", "pick-sibling-co")
    db.add(co)
    await db.flush()

    val = 12_000_000_000
    # The complete sibling — clearly the survivor (typed + dated + amount).
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series C",
            amount_raised=250_000_000,
            announced_date=date(2025, 6, 1),
            valuation_post_money=val,
            primary_news_url="https://techcrunch.com/series-c",
            extraction_confidence="high",
        )
    )
    # The phantom — only the valuation.
    db.add(
        FundingRound(
            company_id=co.id,
            valuation_post_money=val,
            primary_news_url="https://news.google.com/articles/phantom",
        )
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db)
    assert summary.phantom_valuation_rows_merged == 1

    rows = await _rounds_for(db, co.id)
    assert len(rows) == 1
    survivor = rows[0]
    assert survivor.round_type == "Series C"
    assert survivor.amount_raised == 250_000_000
    assert survivor.valuation_post_money == val


async def test_repoints_article_links_onto_survivor(db: AsyncSession) -> None:
    """An article exact-linked (0044) to the loser round follows the merge to
    the survivor instead of being SET-NULLed by the loser's delete."""
    co = _co("Article Linked Co", "article-linked-co")
    db.add(co)
    await db.flush()

    survivor = FundingRound(
        company_id=co.id,
        round_type="Series B",
        amount_raised=60_000_000,
        announced_date=date(2026, 4, 1),
        primary_news_url="https://techcrunch.com/alc-series-b",
        extraction_confidence="high",
    )
    dup = FundingRound(
        company_id=co.id,
        round_type=None,
        amount_raised=60_000_000,
        announced_date=None,
        primary_news_url=None,
        extraction_confidence="low",
    )
    db.add_all([survivor, dup])
    await db.flush()

    article = NewsArticle(
        company_id=co.id,
        url="https://siliconangle.com/alc-coverage",
        title="ALC raises $60M",
        source="siliconangle.com",
        raw_content="body",
        processed=True,
        funding_round_id=dup.id,  # exact-linked to the LOSER
    )
    db.add(article)
    await db.commit()

    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.duplicate_rows_merged >= 1

    await db.refresh(article)
    rounds = await _rounds_for(db, co.id)
    assert len(rounds) == 1  # dup collapsed
    assert article.funding_round_id == rounds[0].id  # link followed the merge


# ── Placeholder round_type normalization (2026-07-16 QA, sambanova) ──────────


def test_normalized_type_maps_placeholders_to_none() -> None:
    from nous.pipeline.repair_duplicate_rounds import _normalized_type

    assert _normalized_type("Series ?") is None
    assert _normalized_type("SERIES") is None
    assert _normalized_type("  unknown  ") is None
    assert _normalized_type("n/a") is None
    assert _normalized_type("") is None
    assert _normalized_type(None) is None
    # Real types keep their identity, case-normalized.
    assert _normalized_type("Series F") == "series f"
    assert _normalized_type("seed") == "seed"
    assert _normalized_type("Venture Round") == "venture round"


async def test_placeholder_typed_row_folds_into_real_round(db: AsyncSession) -> None:
    """sambanova case: 'Series ?' $1B (undated) folds into Series F $1B (dated).

    A placeholder type carries no round identity, so it clusters as untyped and
    the equal-amount merge collapses it into the one real cluster. The survivor
    must keep the REAL type — never inherit the placeholder string.
    """
    co = _co("SambaNova Test", "sambanova-test")
    db.add(co)
    await db.flush()

    real = FundingRound(
        company_id=co.id,
        round_type="Series F",
        amount_raised=1_000_000_000,
        announced_date=date(2026, 7, 8),
        extraction_confidence="high",
    )
    placeholder = FundingRound(
        company_id=co.id,
        round_type="Series ?",
        amount_raised=1_000_000_000,
        announced_date=None,
        extraction_confidence="low",
    )
    db.add_all([real, placeholder])
    await db.commit()

    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.duplicate_rows_merged == 1

    rounds = await _rounds_for(db, co.id)
    assert len(rounds) == 1
    assert rounds[0].round_type == "Series F"
    assert rounds[0].announced_date == date(2026, 7, 8)


async def test_placeholder_only_shell_is_deleted(db: AsyncSession) -> None:
    """A row whose ONLY content is a placeholder type is a signal-free shell."""
    co = _co("Shell Co", "shell-co")
    db.add(co)
    await db.flush()
    db.add(
        FundingRound(
            company_id=co.id,
            round_type="Series ?",
            amount_raised=None,
            announced_date=None,
        )
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.empty_rows_deleted == 1
    assert await _rounds_for(db, co.id) == []


async def test_contradicting_real_types_same_amount_preserved(
    db: AsyncSession,
) -> None:
    """Series E vs Series F at the same $1B do NOT merge (real types contradict).

    Pins the conservative behavior the (future) publication-date-gated fold
    must relax deliberately, never by accident.
    """
    co = _co("Conflicted Co", "conflicted-co")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            FundingRound(
                company_id=co.id,
                round_type="Series F",
                amount_raised=1_000_000_000,
                announced_date=date(2026, 7, 8),
            ),
            FundingRound(
                company_id=co.id,
                round_type="Series E",
                amount_raised=1_000_000_000,
                announced_date=None,
            ),
        ]
    )
    await db.commit()

    await run_repair_duplicate_rounds(db, dry_run=False)
    assert len(await _rounds_for(db, co.id)) == 2


async def test_placeholder_typed_phantom_valuation_collapses(db: AsyncSession) -> None:
    """A 'Series ?'-typed, valuation-only row is a phantom post-normalization.

    Pins the behavior change from placeholder normalization: the placeholder
    string no longer shields a valuation-only shell from Pass 3. It folds into
    the more-complete sibling carrying the SAME valuation (never lost, #107).
    """
    co = _co("Phantom Placeholder Co", "phantom-placeholder-co")
    db.add(co)
    await db.flush()

    real = FundingRound(
        company_id=co.id,
        round_type="Series B",
        amount_raised=200_000_000,
        announced_date=date(2026, 5, 1),
        valuation_post_money=20_000_000_000,
    )
    phantom = FundingRound(
        company_id=co.id,
        round_type="Series ?",
        amount_raised=None,
        announced_date=None,
        valuation_post_money=20_000_000_000,
    )
    db.add_all([real, phantom])
    await db.commit()

    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.phantom_valuation_rows_merged == 1

    rounds = await _rounds_for(db, co.id)
    assert len(rounds) == 1
    assert rounds[0].round_type == "Series B"
    assert rounds[0].valuation_post_money == 20_000_000_000


# ── Pass 2b: near-amount collapse (2026-07-16 QA, terrafirma) ────────────────


async def test_near_amount_merge_terrafirma(db: AsyncSession) -> None:
    """$115M Series A (dated) + $100M Series A (undated) → ONE round at $115M.

    The anchor keeps its OWN amount (figure and citing source travel
    together); the loser's investors repoint to the survivor.
    """
    co = _co("TerraFirma Test", "terrafirma-test")
    kp = Investor(
        name="Kleiner Perkins",
        name_normalized="kleiner perkins",
        slug="kleiner-perkins-na",
    )
    db.add_all([co, kp])
    await db.flush()

    dated = FundingRound(
        company_id=co.id,
        round_type="Series A",
        amount_raised=115_000_000,
        announced_date=date(2026, 7, 14),
        primary_news_url="https://siliconangle.com/terrafirma-115m",
        extraction_confidence="high",
    )
    undated = FundingRound(
        company_id=co.id,
        round_type="Series A",
        amount_raised=100_000_000,
        announced_date=None,
        primary_news_url="https://finsmes.com/terrafirma-100m",
        extraction_confidence="medium",
    )
    db.add_all([dated, undated])
    await db.flush()
    db.add(
        FundingRoundInvestor(
            funding_round_id=undated.id, investor_id=kp.id, is_lead=True
        )
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.near_amount_rows_merged == 1

    rounds = await _rounds_for(db, co.id)
    assert len(rounds) == 1
    survivor = rounds[0]
    assert survivor.amount_raised == 115_000_000
    assert survivor.announced_date == date(2026, 7, 14)
    assert survivor.primary_news_url == "https://siliconangle.com/terrafirma-115m"
    links = (
        await db.execute(
            select(FundingRoundInvestor).where(
                FundingRoundInvestor.funding_round_id == survivor.id
            )
        )
    ).scalars().all()
    assert len(links) == 1 and links[0].is_lead

    # Idempotent: nothing left to merge.
    second = await run_repair_duplicate_rounds(db, dry_run=False)
    assert second.near_amount_rows_merged == 0


async def test_near_amount_no_chaining(db: AsyncSession) -> None:
    """Tolerance anchors to the best row, never chains through intermediates.

    $100M (dated anchor) claims $110M (10%) but NOT $126M (26% of anchor,
    though only 12.7% of $110M — chaining would wrongly swallow it).
    """
    co = _co("Chain Co", "chain-co")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            FundingRound(
                company_id=co.id,
                round_type="Series B",
                amount_raised=100_000_000,
                announced_date=date(2026, 6, 1),
            ),
            FundingRound(
                company_id=co.id,
                round_type="Series B",
                amount_raised=110_000_000,
            ),
            FundingRound(
                company_id=co.id,
                round_type="Series B",
                amount_raised=126_000_000,
            ),
        ]
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.near_amount_rows_merged == 1
    rounds = await _rounds_for(db, co.id)
    amounts = sorted(r.amount_raised for r in rounds)
    assert amounts == [100_000_000, 126_000_000]


async def test_near_amount_contradicting_types_not_merged(db: AsyncSession) -> None:
    co = _co("Two Rounds Co", "two-rounds-co")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            FundingRound(
                company_id=co.id,
                round_type="Series A",
                amount_raised=100_000_000,
                announced_date=date(2026, 1, 10),
            ),
            FundingRound(
                company_id=co.id,
                round_type="Series B",
                amount_raised=110_000_000,
                announced_date=date(2026, 1, 12),
            ),
        ]
    )
    await db.commit()
    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.near_amount_rows_merged == 0
    assert len(await _rounds_for(db, co.id)) == 2


async def test_near_amount_far_dates_not_merged(db: AsyncSession) -> None:
    """Two dated seed rounds months apart stay distinct even at near amounts."""
    co = _co("Tranche Co", "tranche-co")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            FundingRound(
                company_id=co.id,
                round_type="seed",
                amount_raised=10_000_000,
                announced_date=date(2025, 1, 1),
            ),
            FundingRound(
                company_id=co.id,
                round_type="seed",
                amount_raised=11_000_000,
                announced_date=date(2025, 12, 1),
            ),
        ]
    )
    await db.commit()
    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.near_amount_rows_merged == 0
    assert len(await _rounds_for(db, co.id)) == 2


async def test_near_amount_dry_run_writes_nothing(db: AsyncSession) -> None:
    co = _co("Dry Near Co", "dry-near-co")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            FundingRound(
                company_id=co.id,
                round_type="Series A",
                amount_raised=115_000_000,
                announced_date=date(2026, 7, 14),
            ),
            FundingRound(
                company_id=co.id, round_type="Series A", amount_raised=100_000_000
            ),
        ]
    )
    await db.commit()
    summary = await run_repair_duplicate_rounds(db, dry_run=True)
    assert summary.near_amount_rows_merged == 1
    assert len(await _rounds_for(db, co.id)) == 2


# ── Pass 2c: contradicting-type fold on publication-date evidence ────────────


async def test_type_conflict_fold_sambanova(db: AsyncSession) -> None:
    """Series E ($1B, undated, article published next day) folds into the
    dated Series F anchor; Series D whose article published far outside the
    window stays. The anchor's letter wins."""
    co = _co("SambaNova Fold", "sambanova-fold")
    db.add(co)
    await db.flush()

    anchor = FundingRound(
        company_id=co.id,
        round_type="Series F",
        amount_raised=1_000_000_000,
        announced_date=date(2026, 7, 8),
        primary_news_url="https://siliconangle.com/sambanova-1b",
    )
    misletter_near = FundingRound(
        company_id=co.id,
        round_type="Series E",
        amount_raised=1_000_000_000,
        primary_news_url="https://zawya.com/sambanova-series-e",
    )
    misletter_far = FundingRound(
        company_id=co.id,
        round_type="Series D",
        amount_raised=1_000_000_000,
        primary_news_url="https://old.example.com/sambanova-series-d",
    )
    db.add_all([anchor, misletter_near, misletter_far])
    db.add_all(
        [
            NewsArticle(
                company_id=co.id,
                url="https://zawya.com/sambanova-series-e",
                title="QIA participates in SambaNova's $1bln Series E round",
                source="zawya.com",
                published_date=date(2026, 7, 9),
                raw_content="body",
            ),
            NewsArticle(
                company_id=co.id,
                url="https://old.example.com/sambanova-series-d",
                title="SambaNova raises $1B Series D",
                source="old.example.com",
                published_date=date(2025, 4, 1),
                raw_content="body",
            ),
        ]
    )
    await db.commit()

    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.type_conflict_rows_merged == 1

    rounds = await _rounds_for(db, co.id)
    assert len(rounds) == 2
    types = sorted(r.round_type for r in rounds)
    assert types == ["Series D", "Series F"]


async def test_type_conflict_no_article_evidence_stays(db: AsyncSession) -> None:
    """No stored article for the loser's source → never guess, never fold."""
    co = _co("No Evidence Co", "no-evidence-co")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            FundingRound(
                company_id=co.id,
                round_type="Series F",
                amount_raised=1_000_000_000,
                announced_date=date(2026, 7, 8),
            ),
            FundingRound(
                company_id=co.id,
                round_type="Series E",
                amount_raised=1_000_000_000,
                primary_news_url="https://nowhere.example.com/not-stored",
            ),
        ]
    )
    await db.commit()
    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.type_conflict_rows_merged == 0
    assert len(await _rounds_for(db, co.id)) == 2


async def test_type_conflict_two_dated_anchors_skip(db: AsyncSession) -> None:
    """Two dated contradicting-type rows = genuinely ambiguous → untouched."""
    co = _co("Ambiguous Co", "ambiguous-co")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            FundingRound(
                company_id=co.id,
                round_type="Series F",
                amount_raised=1_000_000_000,
                announced_date=date(2026, 7, 8),
            ),
            FundingRound(
                company_id=co.id,
                round_type="Series E",
                amount_raised=1_000_000_000,
                announced_date=date(2026, 7, 9),
            ),
        ]
    )
    await db.commit()
    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.type_conflict_rows_merged == 0
    assert len(await _rounds_for(db, co.id)) == 2


async def test_type_conflict_untyped_anchor_skip(db: AsyncSession) -> None:
    """An untyped dated anchor can't arbitrate contradicting letters."""
    co = _co("Untyped Anchor Co", "untyped-anchor-co")
    db.add(co)
    await db.flush()
    rounds = [
        FundingRound(
            company_id=co.id,
            amount_raised=1_000_000_000,
            announced_date=date(2026, 7, 8),
        ),
        FundingRound(
            company_id=co.id,
            round_type="Series E",
            amount_raised=1_000_000_000,
            primary_news_url="https://a.example.com/e",
        ),
        FundingRound(
            company_id=co.id,
            round_type="Series D",
            amount_raised=1_000_000_000,
            primary_news_url="https://b.example.com/d",
        ),
    ]
    db.add_all(rounds)
    db.add_all(
        [
            NewsArticle(
                company_id=co.id,
                url="https://a.example.com/e",
                title="t1",
                source="a.example.com",
                published_date=date(2026, 7, 9),
                raw_content="body",
            ),
            NewsArticle(
                company_id=co.id,
                url="https://b.example.com/d",
                title="t2",
                source="b.example.com",
                published_date=date(2026, 7, 9),
                raw_content="body",
            ),
        ]
    )
    await db.commit()
    summary = await run_repair_duplicate_rounds(db, dry_run=False)
    assert summary.type_conflict_rows_merged == 0
    # The untyped $1B row folds into ONE typed cluster? No — with two
    # contradicting non-null clusters the untyped row stays separate (Pass 2
    # ambiguity rule), so all three rows must survive.
    assert len(await _rounds_for(db, co.id)) == 3
