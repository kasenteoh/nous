"""unexclude-prominent — retroactive funding-prominence override backfill.

Re-includes companies the AUTOMATED eligibility judge excluded as
``not_a_startup`` that carry a recorded funding round >= ``PROMINENCE_OVERRIDE_USD``
($500M). This is the one-shot backfill counterpart to the in-pipeline override
guards (enrich-companies / judge-eligibility): those keep NEW mega-raiser
verdicts shown; this clears the ones already excluded before the rule existed
(the blue-origin case — owner call 2026-07-20, see nous.util.prominence).

Selection is deliberately narrow: ``exclusion_reason = 'not_a_startup'`` AND a
recorded round >= the threshold. Manual/ops exclusions (``manual``, ``non_us``,
``parse_artifact``) are NEVER touched — operator intent wins.

Dry-run by default: lists each candidate's slug, max recorded round, and the
stored ``exclusion_detail``, writing nothing. ``--apply`` clears the exclusion
(``exclusion_reason``/``exclusion_detail``/``excluded_at`` -> NULL, mirroring
``unexclude-company``) and logs each. Idempotent: a cleared row no longer has
``exclusion_reason = 'not_a_startup'``, so a second run selects nothing.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound
from nous.util.prominence import PROMINENCE_OVERRIDE_USD

logger = logging.getLogger(__name__)


class UnexcludeProminentRow(BaseModel):
    slug: str
    # Largest recorded round, formatted whole USD (e.g. "$650,000,000").
    max_round_usd: str
    exclusion_detail: str | None = None


class UnexcludeProminentSummary(BaseModel):
    dry_run: bool = True
    candidates: int = 0
    # Rows whose exclusion was cleared (0 on a dry-run).
    cleared: int = 0
    companies: list[UnexcludeProminentRow] = Field(default_factory=list)


async def run_unexclude_prominent(
    session: AsyncSession, *, dry_run: bool = True
) -> UnexcludeProminentSummary:
    """Clear the not_a_startup exclusion for prominent-round companies.

    See the module docstring. Returns the candidate list (dry-run) or the
    cleared list (apply). Only ever touches ``exclusion_reason = 'not_a_startup'``
    rows with a recorded round >= ``PROMINENCE_OVERRIDE_USD``.
    """
    max_round = (
        select(
            FundingRound.company_id.label("company_id"),
            func.max(FundingRound.amount_raised).label("max_amount"),
        )
        .group_by(FundingRound.company_id)
        .subquery()
    )
    stmt = (
        select(Company, max_round.c.max_amount)
        .join(max_round, max_round.c.company_id == Company.id)
        .where(Company.exclusion_reason == "not_a_startup")
        .where(max_round.c.max_amount >= PROMINENCE_OVERRIDE_USD)
        .order_by(Company.slug.asc())
    )
    rows = (await session.execute(stmt)).all()

    summary = UnexcludeProminentSummary(dry_run=dry_run, candidates=len(rows))
    for company, max_amount in rows:
        amount = Decimal(max_amount)
        summary.companies.append(
            UnexcludeProminentRow(
                slug=company.slug,
                max_round_usd=f"${amount:,.0f}",
                exclusion_detail=company.exclusion_detail,
            )
        )
        logger.info(
            "unexclude-prominent%s: %s — max round $%s (was not_a_startup: %s)",
            " (dry-run)" if dry_run else "",
            company.slug,
            f"{amount:,.0f}",
            company.exclusion_detail or "—",
        )
        if not dry_run:
            company.exclusion_reason = None
            company.exclusion_detail = None
            company.excluded_at = None
            session.add(company)
            summary.cleared += 1

    if not dry_run and summary.cleared:
        # Clearing exclusion_reason is the whole change — it removes the row from
        # the selection, so an idempotent second run selects nothing.
        await session.commit()
    return summary
