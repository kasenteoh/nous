"""link-competitors pipeline stage.

Fuzzy-resolves the *dangling* foreign keys left behind by analyze-competitors.
That stage only resolves a competitor's ``competitor_company_id`` when the
LLM-emitted name matches a company's ``normalized_name`` *exactly*, so most
edges land name-only (FK NULL) — "Stripe Inc." resolves, but "Stripe Payments"
or a slight misspelling does not. This stage densifies the graph at zero LLM
cost by trigram-matching those leftover names against the GIN-indexed
``companies.normalized_name`` (the same index dedup-companies leans on).

Tie guard. A fuzzy match is only written when it is *decisive*. We pull the
top two companies by trigram similarity; if a second candidate sits within
``tie_margin`` of the best, the name is too generic / too contested to resolve
confidently ("acme" could be any of several rows), so we leave the FK NULL and
count it ``skipped_ambiguous`` rather than guess. We never write our own
company_id (a company can't be its own competitor — there's a DB CHECK), nor a
candidate below ``threshold``.

Idempotency. The selection is strictly ``competitor_company_id IS NULL``: this
stage only ever *fills* a dangling FK, never rewrites a resolved one. A re-run
therefore finds the rows it linked already non-NULL and skips them — fewer rows
each pass, converging to a no-op. Per-row commits keep a mid-run crash (or a
``max_runtime``-less CI cut) on a clean boundary, and a row deleted out from
under us by a concurrent dedup-companies merge raises ``StaleDataError``, which
we tolerate (roll back, move on) exactly as resolve-homepages does.
"""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from nous.db.models import Company, Competitor
from nous.util.slugify import normalize_name

logger = logging.getLogger(__name__)


class LinkCompetitorsSummary(BaseModel):
    rows_seen: int = 0
    linked: int = 0
    skipped_no_match: int = 0
    skipped_ambiguous: int = 0
    skipped_self: int = 0


async def _top_candidates(
    session: AsyncSession, *, name: str, threshold: float
) -> list[tuple[UUID, float]]:
    """Return up to the top 2 companies by trigram similarity to ``name``.

    Mirrors the ``func.similarity`` form in dedup-companies: the pg_trgm GIN
    index on ``Company.normalized_name`` backs the score. Only candidates at or
    above ``threshold`` are returned, highest similarity first, so the caller
    can read ``[0]`` as the best match and ``[1]`` (if present) as the runner-up
    for the tie check.
    """
    sim = func.similarity(Company.normalized_name, name)
    stmt = (
        select(Company.id, sim.label("sim"))
        .where(sim >= threshold)
        .order_by(sim.desc())
        .limit(2)
    )
    result = await session.execute(stmt)
    return [(r.id, float(r.sim)) for r in result]


async def run_link_competitors(
    session: AsyncSession,
    *,
    limit: int | None = None,
    threshold: float = 0.45,
    tie_margin: float = 0.08,
    dry_run: bool = False,
) -> LinkCompetitorsSummary:
    """Fuzzy-resolve dangling ``competitors.competitor_company_id`` FKs.

    For every competitor row whose FK is NULL, trigram-match its
    ``normalize_name``-d ``competitor_name`` against ``companies.normalized_name``
    and, when the match is unambiguous, set the FK to the best company.

    Decision rule per row (see the module docstring for the rationale):

    - normalized name empty → ``skipped_no_match``;
    - no candidate ≥ ``threshold`` → ``skipped_no_match``;
    - single best candidate is the row's own ``company_id`` → ``skipped_self``
      (the table CHECK forbids self-competition);
    - a runner-up exists within ``tie_margin`` of the best → ``skipped_ambiguous``
      (too close to call — leave NULL);
    - otherwise link to the best candidate (``linked``).

    The selection is NULL-FK-only, making the stage idempotent: a re-run finds
    the rows it linked already resolved and skips them. ``dry_run=True`` runs
    every read and decision but performs no writes/commits, so the summary
    reports what *would* change. ``StaleDataError`` (a concurrent dedup merge
    deleting the row) is tolerated row-by-row.
    """
    summary = LinkCompetitorsSummary()

    stmt = select(Competitor).where(Competitor.competitor_company_id.is_(None))
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    for row in rows:
        summary.rows_seen += 1

        name = normalize_name(row.competitor_name)
        if not name:
            summary.skipped_no_match += 1
            continue

        candidates = await _top_candidates(
            session, name=name, threshold=threshold
        )
        if not candidates:
            summary.skipped_no_match += 1
            continue

        best_id, best_sim = candidates[0]

        # A company can't be its own competitor (DB CHECK). When the *best*
        # match is the subject company itself, there's no confident other row
        # to point at — leave the FK NULL.
        if best_id == row.company_id:
            summary.skipped_self += 1
            continue

        # Two near-equally-similar candidates ⇒ the name is too contested to
        # resolve confidently. Don't guess.
        if len(candidates) > 1:
            _second_id, second_sim = candidates[1]
            if best_sim - second_sim < tie_margin:
                summary.skipped_ambiguous += 1
                continue

        summary.linked += 1
        if dry_run:
            continue

        row.competitor_company_id = best_id
        session.add(row)
        try:
            await session.commit()
        except StaleDataError:
            # The competitor row (or its company) was deleted mid-run — almost
            # always a concurrent dedup-companies merge folding the company
            # away. Roll back and move on rather than crash.
            await session.rollback()
            summary.linked -= 1
            summary.skipped_no_match += 1
            logger.warning(
                "Competitor %s disappeared mid-link (likely a concurrent merge)"
                " — skipping.",
                row.id,
            )
            continue

        logger.info(
            "link-competitors: %s -> company %s (sim=%.2f)",
            row.id,
            best_id,
            best_sim,
        )

    return summary
