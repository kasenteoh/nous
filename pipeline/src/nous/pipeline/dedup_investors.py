"""dedup-investors pipeline stage.

Group investors by their canonical name (after alias application from
``canonicalize_investor_name``), pick a survivor per group (the one with
the most links, breaking ties by earliest-created), and merge the rest into
the survivor via ``merge_investors``.

Idempotent: a second run finds no duplicates and is a no-op because after
the first run every investor row has a unique ``name_normalized`` (the
canonical key), and ``upsert_investor`` always uses that key on insert.

Also classifies known VC firm investor rows as ``type='institutional'``
based on the ``FIRM_DISPLAY_NAMES`` map (the set of firms whose portfolios
the pipeline actively scrapes). All other investors stay ``'unknown'``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    CompanyInvestor,
    FundingRound,
    FundingRoundInvestor,
    Investor,
)
from nous.db.upsert import merge_investors
from nous.sources.vc_portfolios import FIRM_DISPLAY_NAMES
from nous.util.investor_name import canonicalize_investor_name

logger = logging.getLogger(__name__)

# Canonical names of firms whose VC portfolios we actively scrape.
# These are unambiguously institutional investors, so we set type='institutional'.
# Built at import time from the display names registered in FIRM_DISPLAY_NAMES.
_INSTITUTIONAL_CANONICALS: frozenset[str] = frozenset(
    canonicalize_investor_name(display_name)
    for display_name in FIRM_DISPLAY_NAMES.values()
)


class DedupInvestorsSummary(BaseModel):
    """Outcome of one dedup-investors run."""

    investors_seen: int = 0
    """Total investor rows inspected."""
    duplicate_groups: int = 0
    """Number of canonical-name groups with more than one row."""
    investors_merged: int = 0
    """Loser rows merged into survivors (survivors are not counted)."""
    type_classifications: int = 0
    """Rows updated from type='unknown' to type='institutional'."""


async def _link_count(session: AsyncSession, investor_id: UUID) -> int:
    """Return the total number of company links for *investor_id*.

    Counts UNION of company_investors and funding_round_investors → companies
    (distinct companies, same as portfolio_count semantics).
    """
    ci_leg = (
        select(CompanyInvestor.company_id.label("company_id"))
        .where(CompanyInvestor.investor_id == investor_id)
    )
    fri_leg = (
        select(FundingRound.company_id.label("company_id"))
        .join(FundingRoundInvestor, FundingRoundInvestor.funding_round_id == FundingRound.id)
        .where(FundingRoundInvestor.investor_id == investor_id)
    )
    union_sq = ci_leg.union(fri_leg).subquery("links")
    result = await session.execute(
        select(func.count()).select_from(union_sq)
    )
    return int(result.scalar_one())


async def _classify_institutional(session: AsyncSession) -> int:
    """Set type='institutional' for investors whose canonical name is a known
    VC firm (in FIRM_DISPLAY_NAMES). Skips rows already set to 'institutional'.

    Returns the number of rows updated.
    """
    if not _INSTITUTIONAL_CANONICALS:
        return 0
    result = await session.execute(
        update(Investor)
        .where(
            Investor.name_normalized.in_(list(_INSTITUTIONAL_CANONICALS)),
            Investor.type != "institutional",
        )
        .values(type="institutional")
        .returning(Investor.id)
    )
    updated = len(result.fetchall())
    if updated:
        logger.info(
            "dedup-investors: set type='institutional' for %d investor rows", updated
        )
    return updated


async def run_dedup_investors(session: AsyncSession) -> DedupInvestorsSummary:
    """Deduplicate investor rows by canonical name (alias-applied).

    For each canonical name that maps to more than one investor row:
    1. Pick the survivor: highest link count first, then oldest (smallest
       ``created_at``) as a tiebreaker.
    2. Merge all other rows (losers) into the survivor via ``merge_investors``,
       which repoints ``company_investors`` + ``funding_round_investors``,
       deduplicates overlapping links, deletes the loser, and calls
       ``refresh_investor_counts``.

    After deduplication, classify known-VC-firm rows as ``type='institutional'``.

    Commit cadence: one commit per duplicate group so a mid-run crash leaves
    the DB in a clean state. The caller may pass a session that already has
    ``join_transaction_mode="create_savepoint"`` (test fixtures do this) — the
    commits then land on SAVEPOINTs, which is fine.

    Returns a :class:`DedupInvestorsSummary`.
    """
    summary = DedupInvestorsSummary()

    # Load all investor rows: id, name_normalized, created_at.
    # We do the grouping in Python (the table is small — O(thousands) at most).
    rows_result = await session.execute(
        select(Investor.id, Investor.name_normalized, Investor.created_at)
        .order_by(Investor.created_at.asc())
    )
    rows = rows_result.all()
    summary.investors_seen = len(rows)

    # Group by canonical name (alias-applied).
    # name_normalized is already the canonical key (set by upsert_investor using
    # canonicalize_investor_name), but rows inserted before this PR may have
    # pre-alias canonical keys. Re-apply canonicalize_investor_name here so we
    # group across both old and new canonical forms.
    from collections import defaultdict
    groups: dict[str, list[tuple[UUID, str, object]]] = defaultdict(list)
    for row in rows:
        investor_id: UUID = row[0]
        name_normalized: str = row[1]
        created_at = row[2]
        # Re-canonicalize to apply the alias map to legacy rows that were
        # stored with a pre-alias canonical key.
        re_canonical = canonicalize_investor_name(name_normalized)
        groups[re_canonical].append((investor_id, name_normalized, created_at))

    for canonical, members in groups.items():
        if len(members) <= 1:
            continue

        summary.duplicate_groups += 1

        # Pick the survivor: most links first; then oldest created_at as tiebreaker.
        # We load link counts on-demand; the number of duplicate groups is small
        # so this doesn't materially change runtime.
        counts: list[tuple[UUID, int, object]] = []
        for investor_id, _norm, created_at in members:
            count = await _link_count(session, investor_id)
            counts.append((investor_id, count, created_at))

        # Sort: descending link count, then ascending created_at (oldest first).
        counts.sort(key=lambda x: (-x[1], x[2]))
        survivor_id = counts[0][0]
        losers = [c[0] for c in counts[1:]]

        logger.info(
            "dedup-investors: merging %d duplicates into survivor %s (canonical=%r)",
            len(losers),
            survivor_id,
            canonical,
        )

        for loser_id in losers:
            await merge_investors(session, survivor_id=survivor_id, loser_id=loser_id)
            summary.investors_merged += 1

        await session.commit()

    # Classify known VC firms as institutional — after dedup so we only
    # update the survivor rows (losers are gone).
    type_updated = await _classify_institutional(session)
    summary.type_classifications = type_updated
    if type_updated:
        await session.commit()

    logger.info(
        "dedup-investors complete: %d groups, %d merged, %d classified",
        summary.duplicate_groups,
        summary.investors_merged,
        summary.type_classifications,
    )
    return summary
