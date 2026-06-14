"""repair-duplicate-rounds pipeline stage — collapse same-round duplicates.

Data-only cleanup (no migration) for the duplicate funding_rounds left by the
historical news backfill. reconcile_funding_round used to match only on
round_type + date proximity, with "both round_type and date null → always
insert" and no amount-based merging, so one round re-reported from many
articles (e.g. Helion's $465M Series G — 1 dated company-site row plus 4
null-date Google-News rows, some with a null round_type) landed as several
rows. Because companies.total_raised_usd is often null, the web page sums
amount_raised across rounds, so 5 × $465M rendered as $2.3B.

reconcile_funding_round is the forward fix (added an equal-amount merge path);
this stage repairs the rows already in the DB.

For each company:

1. DELETE fully-empty junk rows (round_type IS NULL AND announced_date IS NULL
   AND amount_raised IS NULL) — they carry no fact and only inflate the count.

2. Group the remaining rows by ``amount_raised``. Within a non-null-amount
   group, cluster rows whose round_types are COMPATIBLE (equal case-insensitive,
   or null) and collapse each cluster to ONE survivor:
     - rows sharing a non-null round_type form one cluster each;
     - null-typed rows fold into the single non-null cluster when there is
       exactly one (the Helion case), form their own cluster when there are
       none, and stay a separate cluster when there are 2+ non-null clusters
       (ambiguous which round the null row belongs to — never guess).
   Rows with a NULL ``amount_raised`` are left untouched: without an amount they
   carry no merge signal here (reconcile's type+date path owns those), and
   touching them risks collapsing genuinely distinct undated rounds.

Survivor selection within a cluster prefers, in order: a non-null round_type,
then a non-null announced_date, then higher extraction_confidence, then a
non-aggregator ``primary_news_url`` host (a real publisher over a Google-News
/ directory link), then the oldest ``created_at`` (stable tie-break). The
losers' non-null fields are folded into the survivor (gap-fill), their
``funding_round_investors`` are repointed/deduped (respecting the unique
(round, investor) pair, promoting is_lead), then the losers are deleted.

Idempotent: after a run every amount group has at most one row per compatible
cluster and no fully-empty rows remain, so a second run collapses nothing.
Records to pipeline_runs via the CLI. ``--dry-run`` logs intended actions
without writing.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import FundingRound, FundingRoundInvestor
from nous.db.upsert import _CONFIDENCE_RANK, refresh_funding_round_count
from nous.pipeline.extract_funding import _is_junk_source_url

logger = logging.getLogger(__name__)


class RepairDuplicateRoundsSummary(BaseModel):
    companies_seen: int = 0
    companies_repaired: int = 0
    empty_rows_deleted: int = 0
    duplicate_rows_merged: int = 0
    dry_run: bool = False


def _normalized_type(round_type: str | None) -> str | None:
    """Lowercased/stripped round_type for clustering, or None when blank."""
    if round_type is None:
        return None
    stripped = round_type.strip().lower()
    return stripped or None


def _survivor_sort_key(row: FundingRound) -> tuple[int, int, int, int, float]:
    """Sort key picking the BEST row in a cluster as the survivor (min = best).

    Lower is better in every component:
    1. has a non-null round_type (0) over none (1)
    2. has a non-null announced_date (0) over none (1)
    3. higher extraction_confidence (negated rank; unknown sinks to the bottom)
    4. primary_news_url is a real publisher (0) over an aggregator/junk/null (1)
    5. oldest created_at — a stable, deterministic final tie-break

    created_at is a tz-aware datetime; its POSIX timestamp keeps the key a
    plain comparable tuple. created_at is server-defaulted NOT NULL, but guard
    for a not-yet-flushed None defensively.
    """
    has_type = 0 if row.round_type is not None else 1
    has_date = 0 if row.announced_date is not None else 1
    conf_rank = _CONFIDENCE_RANK.get(row.extraction_confidence or "", -1)
    has_good_source = (
        0
        if (row.primary_news_url and not _is_junk_source_url(row.primary_news_url))
        else 1
    )
    created_ts = row.created_at.timestamp() if row.created_at is not None else 0.0
    return (has_type, has_date, -conf_rank, has_good_source, created_ts)


def _fold_loser_into_survivor(survivor: FundingRound, loser: FundingRound) -> None:
    """Gap-fill the survivor from a loser row (one-directional, non-null only).

    Mirrors reconcile_funding_round's merge bias: a real value fills a null,
    higher confidence wins, and primary_news_url is first-write-wins (the
    survivor's earliest/most-stable attribution is kept).
    """
    if survivor.round_type is None and loser.round_type is not None:
        survivor.round_type = loser.round_type
    if survivor.amount_raised is None and loser.amount_raised is not None:
        survivor.amount_raised = loser.amount_raised
    if survivor.valuation_post_money is None and loser.valuation_post_money is not None:
        survivor.valuation_post_money = loser.valuation_post_money
    if survivor.valuation_source is None and loser.valuation_source is not None:
        survivor.valuation_source = loser.valuation_source
    if survivor.announced_date is None and loser.announced_date is not None:
        survivor.announced_date = loser.announced_date
    survivor_rank = _CONFIDENCE_RANK.get(survivor.extraction_confidence or "", -1)
    loser_rank = _CONFIDENCE_RANK.get(loser.extraction_confidence or "", -1)
    if loser_rank > survivor_rank:
        survivor.extraction_confidence = loser.extraction_confidence
    if survivor.primary_news_url is None and loser.primary_news_url is not None:
        survivor.primary_news_url = loser.primary_news_url


def _cluster_amount_group(rows: list[FundingRound]) -> list[list[FundingRound]]:
    """Partition one same-amount group into compatible-round_type clusters.

    Each non-null round_type (case-insensitive) becomes its own cluster. Null-
    typed rows fold into the single non-null cluster when exactly one exists,
    stand alone as one cluster when none do, and stay a SEPARATE cluster when
    2+ non-null clusters exist (it's ambiguous which round an untyped row
    belongs to — keep it rather than attach it arbitrarily). Compatibility is
    the equal-or-null rule from reconcile (_round_types_compatible) made into a
    grouping; it is not transitive (null matches everything), hence this
    bucketing rather than a naive union-find.
    """
    typed: dict[str, list[FundingRound]] = defaultdict(list)
    untyped: list[FundingRound] = []
    for row in rows:
        norm = _normalized_type(row.round_type)
        if norm is None:
            untyped.append(row)
        else:
            typed[norm].append(row)

    clusters: list[list[FundingRound]] = list(typed.values())
    if untyped:
        if len(clusters) == 1:
            clusters[0].extend(untyped)
        else:
            # Zero non-null clusters → all untyped rows are one round.
            # 2+ non-null clusters → keep untyped rows as their own cluster.
            clusters.append(untyped)
    return clusters


async def _repoint_round_investors(
    session: AsyncSession, *, survivor_id: UUID, loser_id: UUID
) -> None:
    """Move a loser round's investor links onto the survivor.

    Respects uq_funding_round_investors_round_investor: promote is_lead on
    links the survivor already has for the same investor, delete the loser's
    now-duplicate links, then repoint the rest. Same pattern as
    merge_investors' funding_round_investors handling.
    """
    survivor_investor_ids_subq = select(FundingRoundInvestor.investor_id).where(
        FundingRoundInvestor.funding_round_id == survivor_id
    )
    loser_lead_investor_ids_subq = select(FundingRoundInvestor.investor_id).where(
        FundingRoundInvestor.funding_round_id == loser_id,
        FundingRoundInvestor.is_lead.is_(True),
    )
    # Promote is_lead on the survivor's link where the loser flags it lead.
    await session.execute(
        update(FundingRoundInvestor)
        .where(
            FundingRoundInvestor.funding_round_id == survivor_id,
            FundingRoundInvestor.investor_id.in_(loser_lead_investor_ids_subq),
        )
        .values(is_lead=True)
    )
    # Drop the loser's links for investors the survivor already covers.
    await session.execute(
        delete(FundingRoundInvestor).where(
            FundingRoundInvestor.funding_round_id == loser_id,
            FundingRoundInvestor.investor_id.in_(survivor_investor_ids_subq),
        )
    )
    # Repoint the remaining loser links to the survivor.
    await session.execute(
        update(FundingRoundInvestor)
        .where(FundingRoundInvestor.funding_round_id == loser_id)
        .values(funding_round_id=survivor_id)
    )


async def run_repair_duplicate_rounds(
    session: AsyncSession, *, dry_run: bool = False
) -> RepairDuplicateRoundsSummary:
    """Collapse same-amount duplicate funding rounds company-by-company.

    Idempotent — a second run finds nothing to collapse or delete.
    """
    summary = RepairDuplicateRoundsSummary(dry_run=dry_run)

    company_ids = (
        (await session.execute(select(FundingRound.company_id).distinct()))
        .scalars()
        .all()
    )

    for company_id in company_ids:
        summary.companies_seen += 1
        rows = (
            (
                await session.execute(
                    select(FundingRound).where(FundingRound.company_id == company_id)
                )
            )
            .scalars()
            .all()
        )

        empty_deleted = 0
        merged_here = 0

        # ── Pass 1: fully-empty junk rows ────────────────────────────────────
        survivors_pool: list[FundingRound] = []
        for row in rows:
            if (
                row.round_type is None
                and row.announced_date is None
                and row.amount_raised is None
            ):
                empty_deleted += 1
                if not dry_run:
                    await session.delete(row)
            else:
                survivors_pool.append(row)

        # ── Pass 2: same-amount, compatible-type collapse ────────────────────
        by_amount: dict[Decimal, list[FundingRound]] = defaultdict(list)
        for row in survivors_pool:
            if row.amount_raised is not None:
                by_amount[row.amount_raised].append(row)

        for group in by_amount.values():
            if len(group) < 2:
                continue
            for cluster in _cluster_amount_group(group):
                if len(cluster) < 2:
                    continue
                ordered = sorted(cluster, key=_survivor_sort_key)
                survivor = ordered[0]
                losers = ordered[1:]
                logger.info(
                    "repair-duplicate-rounds: company=%s amount=%s collapsing "
                    "%d rows into survivor=%s (type=%r date=%s)",
                    company_id,
                    survivor.amount_raised,
                    len(cluster),
                    survivor.id,
                    survivor.round_type,
                    survivor.announced_date,
                )
                merged_here += len(losers)
                if dry_run:
                    continue
                for loser in losers:
                    _fold_loser_into_survivor(survivor, loser)
                    await _repoint_round_investors(
                        session, survivor_id=survivor.id, loser_id=loser.id
                    )
                session.add(survivor)
                # Flush the investor repoints before deleting the loser rows so
                # no FK still points at them.
                await session.flush()
                for loser in losers:
                    await session.delete(loser)

        if empty_deleted or merged_here:
            summary.companies_repaired += 1
            summary.empty_rows_deleted += empty_deleted
            summary.duplicate_rows_merged += merged_here
            if not dry_run:
                await session.flush()
                await refresh_funding_round_count(session, company_id)
                await session.commit()

    return summary
