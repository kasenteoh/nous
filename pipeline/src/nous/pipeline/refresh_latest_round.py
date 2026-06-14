"""refresh-latest-round pipeline stage (Task C0).

Recomputes and persists the denormalized "most recent funding round" columns on
every company — ``companies.latest_round_amount`` / ``latest_round_date`` /
``latest_round_type`` — from the ``funding_rounds`` table.

Why denormalize: the web browse page sorts by biggest raise / most-recent raise
and filters by funding stage / funded-since, but PostgREST cannot ORDER BY (or
paginate) an aggregate over the one-to-many ``funding_rounds`` embed. Flattening
the single most-recent round onto the company row turns each of those into a
plain indexed WHERE/ORDER BY. The columns + their indexes are added in migration
0028, which backfills them with the identical ``DISTINCT ON`` query.

"Most recent" = the round with the greatest ``announced_date`` (NULLS LAST):
a dated round always wins over an undated one; a company whose only round is
undated still gets its type/amount, with ``latest_round_date`` left NULL.

Design notes:
- Set-based and fully idempotent: reset every company's three columns to NULL,
  then set them from a ``DISTINCT ON (company_id)`` subquery. A re-run produces
  the same result. The reset leg is what makes a removed/merged round correctly
  clear a now-stale denormalized value (companies whose last round vanished drop
  out of the subquery and stay NULL).
- No new dedup or write-time logic: this is a pure recompute keyed on
  ``company_id``, exactly mirroring the migration backfill.

Called at the end of:
  - ``extract-funding`` (news path) — rounds may have been added.
  - ``extract-funding-website`` (gap-fill) — rounds may have been added.
  - ``backfill-funding-history`` — multi-round backfill may have added rounds.

Also registered as a standalone CLI stage (``nous refresh-latest-round``) and
wired into pipeline.yml so the columns stay fresh even when run on its own.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import literal, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound

logger = logging.getLogger(__name__)


class RefreshLatestRoundSummary(BaseModel):
    """Outcome of one refresh-latest-round run."""

    companies_with_round: int = 0
    """Number of companies whose latest_round_* was set from a funding round."""


async def refresh_latest_round(session: AsyncSession) -> RefreshLatestRoundSummary:
    """Recompute the denormalized latest_round_* columns for every company.

    Args:
        session: An open async SQLAlchemy session. The caller is responsible for
            committing after this function returns.

    Returns:
        A :class:`RefreshLatestRoundSummary` with the count of companies that
        received a non-null latest round.
    """
    # Most-recent round per company: DISTINCT ON (company_id) ordered by
    # announced_date DESC NULLS LAST, id DESC as a deterministic tiebreak.
    # Mirrors the migration 0028 backfill exactly.
    latest = (
        select(
            FundingRound.company_id.label("company_id"),
            FundingRound.amount_raised.label("amount_raised"),
            FundingRound.announced_date.label("announced_date"),
            FundingRound.round_type.label("round_type"),
        )
        .distinct(FundingRound.company_id)
        .order_by(
            FundingRound.company_id,
            FundingRound.announced_date.desc().nulls_last(),
            FundingRound.id.desc(),
        )
        .subquery("latest_round")
    )

    # Step A: reset every company's denormalized fields to NULL so a company
    # whose last round was removed (delete/merge) drops back to NULL rather than
    # keeping a stale value. null() is the typed SQL NULL literal.
    await session.execute(
        update(Company).values(
            latest_round_amount=null(),
            latest_round_date=null(),
            latest_round_type=null(),
        )
    )

    # Step B: set the fields for companies that have at least one round. The
    # correlated subquery match on company_id is what SQLAlchemy's ORM update
    # supports here (same pattern as refresh-investor-counts).
    rows = (
        await session.execute(
            update(Company)
            .where(Company.id == latest.c.company_id)
            .values(
                latest_round_amount=latest.c.amount_raised,
                latest_round_date=latest.c.announced_date,
                latest_round_type=latest.c.round_type,
            )
            .returning(literal(1))
        )
    ).all()
    companies_with_round = len(rows)

    summary = RefreshLatestRoundSummary(companies_with_round=companies_with_round)
    logger.info(
        "refresh-latest-round: set latest_round_* for %d companies",
        companies_with_round,
    )
    return summary
