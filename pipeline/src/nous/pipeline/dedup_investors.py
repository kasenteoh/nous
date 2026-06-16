"""dedup-investors pipeline stage.

Runs three cleanups over the investors table, in order:

1. **Purge junk rows.** Investor rows whose name is a non-investor placeholder
   (``is_junk_investor_name`` — e.g. "a group of investors", "undisclosed",
   "angel investors"), extracted from article phrasing before the upsert-time
   guard existed. They carry no identity, so the row and its (noise) links are
   deleted outright — nothing is repointed.
2. **Merge duplicates.** Group the remaining investors by canonical name (after
   alias application from ``canonicalize_investor_name``), pick a survivor per
   group (most links, ties broken by earliest-created), and merge the rest into
   the survivor via ``merge_investors``.
3. **Classify type.** Known VC firms (``FIRM_DISPLAY_NAMES``) → ``institutional``;
   individual-looking names (``is_individual_investor_name`` — e.g. "Jeff
   Bezos") → ``angel``; everything else stays ``unknown``.

Idempotent: a second run finds no junk and no duplicates and reclassifies to
the same types, so it is a no-op. After the first run every investor row has a
unique ``name_normalized`` (the canonical key), and ``upsert_investor`` always
uses that key on insert and rejects junk names, so neither problem reappears.
"""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    CompanyInvestor,
    FundingRound,
    FundingRoundInvestor,
    Investor,
)
from nous.db.upsert import merge_investors
from nous.sources.vc_portfolios import FIRM_DISPLAY_NAMES
from nous.util.investor_name import (
    canonicalize_investor_name,
    is_individual_investor_name,
    is_junk_investor_name,
)

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
    """Total investor rows inspected (after the junk purge)."""
    junk_purged: int = 0
    """Placeholder/non-investor rows deleted (e.g. 'a group of investors')."""
    duplicate_groups: int = 0
    """Number of canonical-name groups with more than one row."""
    investors_merged: int = 0
    """Loser rows merged into survivors (survivors are not counted)."""
    type_classifications: int = 0
    """Rows updated from type='unknown' to type='institutional'."""
    angel_classifications: int = 0
    """Rows updated to type='angel' (individual-looking names)."""


async def _purge_junk_investors(session: AsyncSession) -> int:
    """Delete investor rows whose name is a non-investor placeholder.

    Loads every investor's display name + canonical key, flags the junk ones
    with ``is_junk_investor_name``, and deletes the row plus its
    ``company_investors`` and ``funding_round_investors`` links. These rows
    ("a group of investors", "undisclosed", "angel investors", …) carry no
    identity — they are article-phrasing artifacts — so we repoint nothing.

    The link deletes are explicit (not relying on the FK ``ON DELETE CASCADE``)
    so the cleanup is self-documenting and unaffected by future cascade changes.
    Idempotent: once purged, the upsert-time guard keeps junk from returning, so
    a second run finds none.

    Returns the number of investor rows deleted.
    """
    rows = (
        await session.execute(select(Investor.id, Investor.name))
    ).all()
    junk_ids: list[UUID] = [
        row[0] for row in rows if is_junk_investor_name(row[1])
    ]
    if not junk_ids:
        return 0

    await session.execute(
        delete(FundingRoundInvestor).where(
            FundingRoundInvestor.investor_id.in_(junk_ids)
        )
    )
    await session.execute(
        delete(CompanyInvestor).where(CompanyInvestor.investor_id.in_(junk_ids))
    )
    await session.execute(delete(Investor).where(Investor.id.in_(junk_ids)))
    logger.info("dedup-investors: purged %d junk investor rows", len(junk_ids))
    return len(junk_ids)


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


async def _classify_angels(session: AsyncSession) -> int:
    """Set type='angel' for investor rows that look like individuals.

    Runs AFTER ``_classify_institutional`` and only touches rows still typed
    ``'unknown'`` — so a firm already pinned to ``'institutional'`` (whether by
    the known-firm map or a prior run) is never relabeled. A row is flagged when
    ``is_individual_investor_name`` returns True: a 2-3 token human-style name
    with a recognized given first name and no firm-marker token (see that
    helper for the full, deliberately conservative heuristic). Known scraped
    firms are additionally guarded via ``known_firm`` so a registry firm can
    never be called an angel even if its name shape were ambiguous.

    The decision is per-row in Python (the table is small) because the heuristic
    is richer than a SQL predicate; the write is one bulk UPDATE keyed by id.

    Returns the number of rows updated.
    """
    rows = (
        await session.execute(
            select(Investor.id, Investor.name, Investor.name_normalized).where(
                Investor.type == "unknown"
            )
        )
    ).all()
    angel_ids: list[UUID] = [
        row[0]
        for row in rows
        if is_individual_investor_name(
            row[1], known_firm=row[2] in _INSTITUTIONAL_CANONICALS
        )
    ]
    if not angel_ids:
        return 0
    await session.execute(
        update(Investor).where(Investor.id.in_(angel_ids)).values(type="angel")
    )
    logger.info(
        "dedup-investors: set type='angel' for %d investor rows", len(angel_ids)
    )
    return len(angel_ids)


async def run_dedup_investors(session: AsyncSession) -> DedupInvestorsSummary:
    """Purge junk rows, deduplicate by canonical name, then classify type.

    0. **Purge junk** (``_purge_junk_investors``): delete placeholder rows like
       "a group of investors" / "undisclosed" and their (noise) links, BEFORE
       grouping so they never become a survivor or skew a merge.
    1. For each canonical name that maps to more than one remaining investor:
       a. Pick the survivor: highest link count first, then oldest (smallest
          ``created_at``) as a tiebreaker.
       b. Merge all other rows (losers) into the survivor via
          ``merge_investors``, which repoints ``company_investors`` +
          ``funding_round_investors``, deduplicates overlapping links, deletes
          the loser, and calls ``refresh_investor_counts``.
    2. Classify type: known-VC-firm rows → ``'institutional'``, then
       individual-looking rows → ``'angel'`` (only rows still ``'unknown'``).

    Commit cadence: one commit after the purge, one per duplicate group, and one
    after each classification step — so a mid-run crash leaves the DB clean. The
    caller may pass a session that already has
    ``join_transaction_mode="create_savepoint"`` (test fixtures do this) — the
    commits then land on SAVEPOINTs, which is fine.

    Returns a :class:`DedupInvestorsSummary`.
    """
    summary = DedupInvestorsSummary()

    # Step 0: purge non-investor placeholder rows before anything else, so a
    # junk row can never win a survivor election or distort a merge. Committed
    # immediately so the deletes are durable even if a later group fails.
    summary.junk_purged = await _purge_junk_investors(session)
    if summary.junk_purged:
        await session.commit()

    # Load all (remaining) investor rows: id, name_normalized, created_at.
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

    # Classify individual-looking rows as angels — after the institutional pass
    # so a known firm is already pinned and only 'unknown' rows are considered.
    angels_updated = await _classify_angels(session)
    summary.angel_classifications = angels_updated
    if angels_updated:
        await session.commit()

    logger.info(
        "dedup-investors complete: %d purged, %d groups, %d merged, "
        "%d institutional, %d angel",
        summary.junk_purged,
        summary.duplicate_groups,
        summary.investors_merged,
        summary.type_classifications,
        summary.angel_classifications,
    )
    return summary
