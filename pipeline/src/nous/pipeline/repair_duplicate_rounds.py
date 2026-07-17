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

1. DELETE fully-empty junk rows — those carrying NO funding signal at all:
   round_type IS NULL AND announced_date IS NULL AND amount_raised IS NULL AND
   valuation_post_money IS NULL AND valuation_source IS NULL.
   A row with only a valuation (e.g. "Company X valued at $2B" from an article
   that gave no round amount) is a REAL sourced fact and must be preserved.

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

3. Collapse valuation-only PHANTOM rows. A phantom is a round with NO
   round_type, NO announced_date and NO amount_raised — only a
   ``valuation_post_money`` (the shape seen on Perplexity's page: blank rows
   carrying just "$20B post-money" beside the real $20B round). When another
   round for the SAME company carries the SAME ``valuation_post_money`` and is
   "more complete" (has an amount OR a type OR a date), the phantom's valuation
   is folded into that sibling — a no-op data-wise, since the valuation is
   already equal, but it keeps the valuation on a real row — its investor links
   are repointed/deduped, and the phantom shell is deleted. A phantom whose
   valuation matches NO sibling is LEFT ALONE: it may be the sole carrier of
   that valuation, and PR #107's "never lose a valuation" invariant forbids
   dropping it. A phantom is never merged into another phantom (the survivor
   must be more complete), so the valuation always lands on a real round.

Survivor selection within a cluster prefers, in order: a non-null round_type,
then a non-null announced_date, then higher extraction_confidence, then a
non-aggregator ``primary_news_url`` host (a real publisher over a Google-News
/ directory link), then the oldest ``created_at`` (stable tie-break). The
losers' non-null fields are folded into the survivor (gap-fill), their
``funding_round_investors`` are repointed/deduped (respecting the unique
(round, investor) pair, promoting is_lead), then the losers are deleted.

Idempotent: after a run every amount group has at most one row per compatible
cluster, no fully-empty rows remain, and every valuation-only phantom either
has been folded into its matching sibling or has no sibling to fold into, so a
second run collapses nothing. Records to pipeline_runs via the CLI.
``--dry-run`` logs intended actions without writing.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import FundingRound, FundingRoundInvestor, NewsArticle
from nous.db.upsert import _CONFIDENCE_RANK, refresh_funding_round_count
from nous.pipeline.extract_funding import _is_junk_source_url

logger = logging.getLogger(__name__)


class RepairDuplicateRoundsSummary(BaseModel):
    companies_seen: int = 0
    companies_repaired: int = 0
    empty_rows_deleted: int = 0
    duplicate_rows_merged: int = 0
    # Pass 3: valuation-only phantom shells folded into a matching sibling.
    phantom_valuation_rows_merged: int = 0
    dry_run: bool = False


# round_type strings that carry NO discriminating signal — placeholder text an
# outlet (or the extraction) used where the series letter was unknown. Treating
# them as typed would block a merge with the real round ("Series ?" $1B vs
# "Series F" $1B are the same event reported by an outlet that didn't know the
# letter — observed on sambanova, 2026-07-16 QA). Normalized to None so the
# equal-or-null compatibility rule applies. Deliberately NOT included: real
# generic types that still discriminate ("seed", "venture round", "grant").
_PLACEHOLDER_ROUND_TYPES: frozenset[str] = frozenset(
    {
        "series ?",
        "series",
        "round",
        "funding round",
        "unknown",
        "undisclosed",
        "unspecified",
        "n/a",
        "none",
        "?",
        "tbd",
    }
)


def _normalized_type(round_type: str | None) -> str | None:
    """Lowercased/stripped round_type for clustering, or None when blank.

    Placeholder strings that carry no round identity ("Series ?", "unknown")
    also normalize to None — see ``_PLACEHOLDER_ROUND_TYPES``.
    """
    if round_type is None:
        return None
    stripped = round_type.strip().lower()
    if not stripped or stripped in _PLACEHOLDER_ROUND_TYPES:
        return None
    return stripped


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
    has_type = 0 if _normalized_type(row.round_type) is not None else 1
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
    # Placeholder types ("Series ?") normalize to None for clustering; never
    # gap-fill a placeholder string onto a survivor either.
    if survivor.round_type is None and _normalized_type(loser.round_type) is not None:
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


def _is_phantom_valuation_row(row: FundingRound) -> bool:
    """True for a valuation-only PHANTOM shell.

    No round_type, no announced_date, no amount_raised — only a
    ``valuation_post_money``. This is the junk Funding-History row seen on
    Perplexity: a blank row that carries nothing but a "$20B post-money" figure.
    It survives Pass 1 (a valuation is a real sourced fact) and Pass 2 ignores
    it (no amount → no merge signal), so Pass 3 handles it. A row with a
    ``valuation_source`` but a null ``valuation_post_money`` is NOT a phantom
    here: there is no numeric valuation to match against a sibling, so it is
    left untouched.
    """
    return (
        _normalized_type(row.round_type) is None
        and row.announced_date is None
        and row.amount_raised is None
        and row.valuation_post_money is not None
    )


def _is_more_complete_round(row: FundingRound) -> bool:
    """True when a row carries real round substance beyond a bare valuation.

    A valid merge TARGET for a phantom: it has an amount, a round_type, or a
    date, so folding the phantom's (equal) valuation onto it lands the valuation
    on a genuine round rather than another empty shell.
    """
    return (
        row.amount_raised is not None
        or _normalized_type(row.round_type) is not None
        or row.announced_date is not None
    )


async def _repoint_round_investors(
    session: AsyncSession, *, survivor_id: UUID, loser_id: UUID
) -> None:
    """Move a loser round's investor links AND article links onto the survivor.

    Investor links respect uq_funding_round_investors_round_investor: promote
    is_lead on links the survivor already has for the same investor, delete the
    loser's now-duplicate links, then repoint the rest. Same pattern as
    merge_investors' funding_round_investors handling.

    Article links (news_articles.funding_round_id, migration 0044) repoint
    wholesale — without this, deleting the loser would SET NULL them and only
    the survivor's primary article re-heals via repair-catalog pass 4; the
    non-primary coverage would permanently fall back to date-proximity
    grouping (which cannot group under undated rounds).
    """
    await session.execute(
        update(NewsArticle)
        .where(NewsArticle.funding_round_id == loser_id)
        .values(funding_round_id=survivor_id)
    )
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


async def _collapse_phantom_valuations(
    session: AsyncSession,
    *,
    company_id: UUID,
    rows: list[FundingRound],
    dry_run: bool,
) -> int:
    """Pass 3 — fold valuation-only phantom shells into a matching sibling.

    For each ``valuation_post_money`` value on the company, if there is at least
    one phantom row carrying it (see ``_is_phantom_valuation_row``) AND at least
    one "more complete" sibling carrying the SAME valuation (see
    ``_is_more_complete_round``), every such phantom is folded into the best
    sibling: the sibling already holds the (equal) valuation so PR #107's "never
    lose a valuation" invariant is preserved, the phantom's investor links are
    repointed/deduped, and the phantom is deleted.

    Conservative by construction:
    - a phantom whose valuation matches no sibling is never touched (it might be
      the only carrier of that valuation);
    - the survivor is always a "more complete" row, never another phantom, so
      the valuation never gets stranded on a second empty shell;
    - it runs over ``survivors_pool`` AFTER Pass 2, so a row already chosen as a
      same-amount survivor (which has an amount, hence is not a phantom) is a
      valid target, not a victim.

    Mutates ``rows`` in place — collapsed phantoms are removed from the list so
    the caller's count refresh and idempotency hold. Returns the number of
    phantom rows merged.
    """
    # Bucket every valuation-bearing row by its exact post-money figure.
    by_valuation: dict[Decimal, list[FundingRound]] = defaultdict(list)
    for row in rows:
        if row.valuation_post_money is not None:
            by_valuation[row.valuation_post_money].append(row)

    merged = 0
    collapsed_ids: set[UUID] = set()
    for valuation, group in by_valuation.items():
        phantoms = [r for r in group if _is_phantom_valuation_row(r)]
        if not phantoms:
            continue
        # Candidate survivors: same valuation AND real round substance. Never a
        # phantom — that would just move the valuation to another empty shell.
        candidates = [r for r in group if _is_more_complete_round(r)]
        if not candidates:
            # Lone/duplicated phantom valuation with no complete sibling — the
            # #107 invariant says keep it; it may be the only carrier.
            continue
        survivor = sorted(candidates, key=_survivor_sort_key)[0]

        for phantom in phantoms:
            logger.info(
                "repair-duplicate-rounds: company=%s collapsing phantom "
                "valuation row=%s (valuation=%s) into sibling=%s",
                company_id,
                phantom.id,
                valuation,
                survivor.id,
            )
            merged += 1
            if dry_run:
                continue
            # Fold first (valuation already equal → no-op, but keeps the
            # invariant explicit and gap-fills valuation_source if the phantom
            # has one the survivor lacks), then repoint investors.
            _fold_loser_into_survivor(survivor, phantom)
            await _repoint_round_investors(
                session, survivor_id=survivor.id, loser_id=phantom.id
            )
            collapsed_ids.add(phantom.id)

        if not dry_run:
            session.add(survivor)
            # Flush the investor repoints before deleting the phantoms so no FK
            # still points at them.
            await session.flush()
            for phantom in phantoms:
                await session.delete(phantom)

    if collapsed_ids:
        rows[:] = [r for r in rows if r.id not in collapsed_ids]
    return merged


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
        # A row is only junk if it carries NO funding signal at all.
        # A valuation-only row (valuation_post_money or valuation_source set,
        # but round_type/announced_date/amount_raised all null) is a real sourced
        # fact — e.g. "Company X valued at $2B" from an article that stated no
        # round amount — and must NOT be deleted here.
        survivors_pool: list[FundingRound] = []
        for row in rows:
            if (
                # Placeholder-only types ("Series ?") carry no signal either.
                _normalized_type(row.round_type) is None
                and row.announced_date is None
                and row.amount_raised is None
                and row.valuation_post_money is None
                and row.valuation_source is None
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
                # Keep survivors_pool in sync so Pass 3 never buckets a row this
                # pass just deleted (loser rows have an amount, so they are not
                # phantoms, but a deleted loser must not appear as a sibling).
                loser_ids = {loser.id for loser in losers}
                survivors_pool = [
                    r for r in survivors_pool if r.id not in loser_ids
                ]

        # ── Pass 3: valuation-only phantom shells ────────────────────────────
        # Fold each phantom (no type/date/amount, only a valuation_post_money)
        # into a "more complete" sibling carrying the SAME valuation. The
        # valuation is already equal, so this is data-preserving — PR #107's
        # "never lose a valuation" invariant holds because it lands on a real
        # round. Phantoms with no matching sibling are left alone.
        phantom_merged = await _collapse_phantom_valuations(
            session,
            company_id=company_id,
            rows=survivors_pool,
            dry_run=dry_run,
        )

        if empty_deleted or merged_here or phantom_merged:
            summary.companies_repaired += 1
            summary.empty_rows_deleted += empty_deleted
            summary.duplicate_rows_merged += merged_here
            summary.phantom_valuation_rows_merged += phantom_merged
            if not dry_run:
                await session.flush()
                await refresh_funding_round_count(session, company_id)
                await session.commit()

    return summary
