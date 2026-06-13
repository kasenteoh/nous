"""refresh-investor-counts pipeline stage.

Recomputes and persists the denormalized ``investors.portfolio_count`` for
every investor — the count of distinct non-excluded companies that investor
backs via EITHER ``company_investors`` (VC portfolio links) OR
``funding_round_investors`` → ``funding_rounds`` (news-extracted rounds).

Design notes:
- The UNION (not UNION ALL) deduplicates (investor_id, company_id) pairs so a
  company that is linked via BOTH tables is counted only once.
- Excluded companies (exclusion_reason IS NOT NULL) are never counted — they
  are not rendered in the catalog, so an investor "backing" only excluded
  companies should show 0.
- Investors with no qualifying links are explicitly set to 0 so a re-run after
  exclusions does not leave stale positive counts.
- Fully idempotent: the UPDATE is a pure recompute keyed on investor.id; a
  re-run produces the same result.

Called at the end of:
  - ``refresh-vc-portfolios`` — VC links may have changed.
  - ``extract-funding`` — round-level investor links may have been added.

Also registered as a standalone CLI stage (``nous refresh-investor-counts``)
and in discovery.yml so it runs even when refresh-vc-portfolios is skipped.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    CompanyInvestor,
    FundingRound,
    FundingRoundInvestor,
    Investor,
)

logger = logging.getLogger(__name__)


class RefreshInvestorCountsSummary(BaseModel):
    """Outcome of one refresh-investor-counts run."""

    investors_updated: int = 0
    """Number of investor rows whose portfolio_count changed (including set-to-zero)."""


async def refresh_investor_counts(session: AsyncSession) -> RefreshInvestorCountsSummary:
    """Recompute portfolio_count for every investor from first principles.

    Uses two SQLAlchemy core subqueries whose UNION mirrors the SQL in
    migration 0025. The outer UPDATE sets every investor's count, including
    zero for investors with no non-excluded company links.

    Args:
        session: An open async SQLAlchemy session. The caller is responsible
            for committing after this function returns.

    Returns:
        A :class:`RefreshInvestorCountsSummary` with the count of rows updated.
    """
    # --- Build the two legs of the UNION ------------------------------------ #

    # Leg 1: company_investors (VC portfolio links)
    ci_leg = (
        select(
            CompanyInvestor.investor_id.label("inv_id"),
            CompanyInvestor.company_id.label("company_id"),
        )
        .join(Company, Company.id == CompanyInvestor.company_id)
        .where(Company.exclusion_reason.is_(None))
    )

    # Leg 2: funding_round_investors → funding_rounds (news-extracted rounds)
    fri_leg = (
        select(
            FundingRoundInvestor.investor_id.label("inv_id"),
            FundingRound.company_id.label("company_id"),
        )
        .join(FundingRound, FundingRound.id == FundingRoundInvestor.funding_round_id)
        .join(Company, Company.id == FundingRound.company_id)
        .where(Company.exclusion_reason.is_(None))
    )

    # UNION deduplicates (inv_id, company_id) pairs present in both legs.
    union_cte = ci_leg.union(fri_leg).subquery("union_links")

    # --- Per-investor count ------------------------------------------------- #
    counts_cte = (
        select(
            union_cte.c.inv_id,
            func.count(union_cte.c.company_id.distinct()).label("n"),
        )
        .group_by(union_cte.c.inv_id)
        .subquery("investor_counts")
    )

    # --- Update every investor (0 for those absent from the counts subquery) - #
    # We need a two-step approach compatible with SQLAlchemy's ORM update:
    # first zero-out all, then set the non-zero ones.
    # This avoids needing a correlated LEFT JOIN in a single UPDATE, which
    # SQLAlchemy's ORM update() does not support directly with subqueries.

    # Step A: reset all investors to 0.
    await session.execute(update(Investor).values(portfolio_count=0))

    # Step B: set the non-zero counts (those with qualifying links).
    rows = (
        await session.execute(
            update(Investor)
            .where(Investor.id == counts_cte.c.inv_id)
            .values(portfolio_count=counts_cte.c.n)
            .returning(literal(1))
        )
    ).all()
    investors_with_links = len(rows)

    summary = RefreshInvestorCountsSummary(investors_updated=investors_with_links)
    logger.info(
        "refresh-investor-counts: updated portfolio_count for %d investors",
        investors_with_links,
    )
    return summary
