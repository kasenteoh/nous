"""compute-completeness: the stored per-company completeness score for the web.

Writes ``companies.completeness_score`` + ``completeness_computed_at`` (migration
0042) for every shown company, using ``util.completeness.completeness_score`` —
the SAME scorer the internal data-quality report aggregates, so the badge the web
renders and the report can never disagree and the web never re-derives the score
in TS. $0, deterministic, idempotent: one batched read of the shown cohort + the
people set, pure arithmetic, no LLM / network / scikit-learn. A same-DB-state
re-run rewrites byte-identical scores (only ``completeness_computed_at``
re-stamps).

Unlike momentum, the score is never NULL for a shown company — a husk scores 0.0,
not NULL — so every shown company gets a number each run (one that loses a field
drops to a lower score rather than keeping a stale one). The web gates its
positive-only badge on the value (≥0.5), so a husk simply shows no badge.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Person
from nous.util.completeness import completeness_fields, completeness_score

logger = logging.getLogger(__name__)

# Companies per commit. Batched begin_nested + commit so a crash keeps every
# finished batch (mirrors compute-momentum's MOMENTUM_BATCH_SIZE).
COMPLETENESS_BATCH_SIZE: int = 500


class ComputeCompletenessSummary(BaseModel):
    """Result of one compute-completeness run."""

    companies_seen: int = 0  # shown companies processed
    companies_scored: int = 0  # completeness_score written (== seen: never NULL)
    mean_score: float | None = None  # mean over scored companies (display/log only)


async def _shown_companies(session: AsyncSession) -> list[Company]:
    """The catalog "shown" cohort, id-ordered (deterministic run order).

    Mirrors ``compute_momentum._shown_companies`` (and the web catalog bar): not
    soft-excluded AND has either a short description or at least one funding round.
    Kept as this stage's own selection per the codebase idiom (each stage defines
    its cohort inline) so completeness never depends on the momentum module.
    """
    stmt = (
        select(Company)
        .where(
            Company.exclusion_reason.is_(None),
            or_(
                Company.description_short.is_not(None),
                Company.funding_round_count > 0,
            ),
        )
        .order_by(Company.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _company_ids_with_people(session: AsyncSession) -> set[UUID]:
    """Company ids with ≥1 person (one small distinct query) — the has_people
    input. Mirrors data_quality's people-membership check."""
    return set(
        (await session.execute(select(Person.company_id).distinct())).scalars().all()
    )


async def run_compute_completeness(
    session: AsyncSession,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> ComputeCompletenessSummary:
    """(Re)score every shown company's completeness, writing completeness_score +
    completeness_computed_at for ALL of them so one that loses a field drops to a
    lower score rather than keeping a stale one. Batched begin_nested commits: a
    crash keeps every finished batch.

    ``limit`` (default all) caps the run to the first N id-ordered shown companies
    — an operational escape hatch (testing / partial runs); the production path
    scores the whole cohort. ``now`` (defaults to wall-clock UTC) is the
    completeness_computed_at stamp. Deterministic given DB state: the score is a
    pure function of current field presence, so a same-state re-run is
    byte-identical in completeness_score.
    """
    now = now or datetime.now(UTC)
    summary = ComputeCompletenessSummary()

    companies = await _shown_companies(session)
    if limit is not None:
        companies = companies[:limit]
    summary.companies_seen = len(companies)
    people_ids = await _company_ids_with_people(session)

    scores: list[float] = []
    for start in range(0, len(companies), COMPLETENESS_BATCH_SIZE):
        batch = companies[start : start + COMPLETENESS_BATCH_SIZE]
        async with session.begin_nested():
            for company in batch:
                fields = completeness_fields(
                    website=company.website,
                    description_short=company.description_short,
                    funding_round_count=company.funding_round_count,
                    hq_country=company.hq_country,
                    hq_city=company.hq_city,
                    industry_group=company.industry_group,
                    has_people=company.id in people_ids,
                    logo_url=company.logo_url,
                    tags=company.tags,
                    employee_count_min=company.employee_count_min,
                    employee_count_max=company.employee_count_max,
                )
                score = completeness_score(fields)
                company.completeness_score = score
                company.completeness_computed_at = now
                session.add(company)
                scores.append(score)
                summary.companies_scored += 1
        await session.commit()

    if scores:
        summary.mean_score = round(sum(scores) / len(scores), 4)

    logger.info(
        "compute-completeness: seen=%d scored=%d mean=%s",
        summary.companies_seen,
        summary.companies_scored,
        summary.mean_score,
    )
    return summary
