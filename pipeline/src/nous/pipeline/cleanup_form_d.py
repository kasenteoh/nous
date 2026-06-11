"""cleanup-form-d pipeline stage (one-time migration).

SEC Form D ingestion was removed from the pipeline (code + schema), but rows
discovered through it may still sit in the DB carrying ``discovered_via='form_d'``.
This stage reconciles those legacy rows against the *independent* evidence that
the surviving stages produce, then prunes whatever has none.

Rationale — re-tag, then delete:

A Form-D-discovered company is worth keeping only if some other source has since
corroborated it. Two such signals exist, and we apply them strongest-first so a
company ends up tagged by its best provenance:

1. **VC-portfolio evidence (strongest).** ``refresh-vc-portfolios`` writes a
   ``company_investors`` row when a company appears in a tracked firm's
   portfolio. A firm putting a company in its portfolio is the most reliable
   independent confirmation we have, so any ``form_d`` company with such a link
   is re-tagged to ``'vc_portfolio'``.

2. **News evidence.** A company covered by the news pipeline has a
   ``news_articles`` row and/or a ``funding_rounds`` row. Any remaining
   ``form_d`` company with either is re-tagged to ``'news'``.

3. **Delete the rest.** A ``form_d`` row with no investor, news, or funding
   evidence has no independent corroboration; we delete it. Child rows are
   removed automatically by the existing ``ON DELETE CASCADE`` foreign keys.

Order matters: phase 1 runs before phase 2 so a company with *both* an investor
link and news lands on the stronger ``'vc_portfolio'`` tag. After a real run no
``discovered_via='form_d'`` rows remain, so the stage is idempotent — a second
run re-tags 0, deletes 0.

``dry_run=True`` computes the same counts via SELECTs but performs no UPDATE or
DELETE, so an operator can preview the impact before committing. The summary
counts are always derived from ``SELECT COUNT(*)`` over the same predicates the
writes use, taken *before* each write executes — phase N's count is measured
against the set the write will touch (e.g. the news count excludes rows phase 1
already re-tagged), so dry-run and real-run counts agree exactly.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import ColumnElement, delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from nous.db.models import (
    Company,
    CompanyInvestor,
    FundingRound,
    NewsArticle,
)

logger = logging.getLogger(__name__)


class CleanupFormDSummary(BaseModel):
    retagged_vc_portfolio: int = 0
    retagged_news: int = 0
    deleted: int = 0


# ---------------------------------------------------------------------------
# EXISTS predicates — correlated subqueries against the evidence tables.
# Each is a ``WHERE EXISTS (SELECT 1 FROM <child> WHERE child.company_id =
# companies.id)`` so a single bulk statement can re-tag / delete in one pass.
# ---------------------------------------------------------------------------


def _has_investor() -> Exists:
    return exists().where(CompanyInvestor.company_id == Company.id)


def _has_news() -> Exists:
    return exists().where(NewsArticle.company_id == Company.id)


def _has_funding() -> Exists:
    return exists().where(FundingRound.company_id == Company.id)


def _has_news_or_funding() -> ColumnElement[bool]:
    return _has_news() | _has_funding()


async def _count_form_d_where(
    session: AsyncSession, predicate: ColumnElement[bool] | Exists
) -> int:
    """Count ``discovered_via='form_d'`` companies also matching ``predicate``."""
    stmt = (
        select(func.count())
        .select_from(Company)
        .where(Company.discovered_via == "form_d")
        .where(predicate)
    )
    return int((await session.execute(stmt)).scalar_one())


async def run_cleanup_form_d(
    session: AsyncSession,
    *,
    dry_run: bool = False,
) -> CleanupFormDSummary:
    """Re-tag corroborated Form-D companies, delete the rest.

    See the module docstring for the re-tag-then-delete rationale. Returns a
    :class:`CleanupFormDSummary` of the rows affected in each phase. Counts are
    measured via ``SELECT COUNT(*)`` against each phase's predicate *before* the
    corresponding write, so the same numbers are reported in ``dry_run`` mode.
    """
    summary = CleanupFormDSummary()

    # Phase 1 — VC-portfolio evidence (strongest). Measure, then re-tag.
    summary.retagged_vc_portfolio = await _count_form_d_where(
        session, _has_investor()
    )
    if not dry_run and summary.retagged_vc_portfolio:
        await session.execute(
            update(Company)
            .where(Company.discovered_via == "form_d")
            .where(_has_investor())
            .values(discovered_via="vc_portfolio")
        )

    # Phase 2 — news/funding among the *remaining* form_d rows.
    # In a real run phase 1's rows are now 'vc_portfolio' so the
    # ``discovered_via='form_d'`` filter already excludes them. In dry-run they
    # are still 'form_d', so we additionally subtract any investor-linked rows
    # (``& ~_has_investor()``) to mirror the post-phase-1 set and keep the count
    # identical to what a real run would re-tag.
    news_predicate = _has_news_or_funding()
    if dry_run:
        summary.retagged_news = await _count_form_d_where(
            session, news_predicate & ~_has_investor()
        )
    else:
        summary.retagged_news = await _count_form_d_where(session, news_predicate)
        if summary.retagged_news:
            await session.execute(
                update(Company)
                .where(Company.discovered_via == "form_d")
                .where(news_predicate)
                .values(discovered_via="news")
            )

    # Phase 3 — delete every remaining 'form_d' row (child rows cascade).
    # The remaining set has neither investor nor news/funding evidence.
    summary.deleted = await _count_form_d_where(
        session, ~_has_investor() & ~_has_news_or_funding()
    )
    if not dry_run and summary.deleted:
        await session.execute(
            delete(Company)
            .where(Company.discovered_via == "form_d")
            .where(~_has_investor())
            .where(~_has_news_or_funding())
        )

    if dry_run:
        logger.info(
            "cleanup-form-d (dry-run): would re-tag %d → vc_portfolio, %d → "
            "news, and delete %d",
            summary.retagged_vc_portfolio,
            summary.retagged_news,
            summary.deleted,
        )
        return summary

    await session.commit()
    logger.info(
        "cleanup-form-d: re-tagged %d → vc_portfolio, %d → news, deleted %d",
        summary.retagged_vc_portfolio,
        summary.retagged_news,
        summary.deleted,
    )
    return summary
