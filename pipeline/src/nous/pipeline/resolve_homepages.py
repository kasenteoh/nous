"""resolve-homepages pipeline stage.

For each company that has no website (and was never attempted, or whose
attempt is older than the refetch window), try CANDIDATE_TLDS to find a live
homepage and record the result. Companies that already have a website — e.g.
provided by a VC portfolio adapter — are never re-resolved: doing so wastes
~13s of wall clock per company across the whole table and risks overwriting
a correct discovery-provided URL with a wrong TLD guess.

Commit cadence: one commit per company so a mid-run crash leaves a clean
state, and so a ``max_runtime_minutes`` budget can stop the loop at any
company boundary — the next run's selection naturally resumes where this one
stopped (website_resolved_at IS NULL ⇒ not yet attempted).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from nous.db.models import Company
from nous.sources.homepage import HomepageClient, resolve_homepage
from nous.util.slugify import slugify

logger = logging.getLogger(__name__)


class ResolveSummary(BaseModel):
    companies_seen: int = 0
    websites_resolved: int = 0
    websites_unchanged: int = 0
    no_match: int = 0
    # Subset of no_match recorded while DDG's circuit breaker was open: only
    # the TLD heuristic ran. These verdicts are weaker — re-attempt early via
    # --refetch-after-days if this number is large.
    no_match_ddg_blocked: int = 0
    errors: int = 0
    # True when the max_runtime_minutes budget stopped the loop before the
    # selection was drained. The remaining companies stay eligible next run.
    stopped_early: bool = False


async def run_resolve_homepages(
    session: AsyncSession,
    client: HomepageClient,
    *,
    refetch_after_days: int = 90,
    limit: int | None = None,
    max_runtime_minutes: float | None = None,
) -> ResolveSummary:
    """For each company with no website whose last attempt is absent or stale,
    attempt to resolve a homepage via common TLDs (+ DDG fallback).

    On a non-None result, update company.website + company.website_resolved_at.
    On None, still set website_resolved_at (so we don't retry every run);
    leave website as-is.

    ``max_runtime_minutes`` is a clean-exit wall-clock budget: the loop stops
    at the next company boundary once exceeded. Combined with per-company
    commits this makes the stage resumable across bounded CI runs.
    """
    summary = ResolveSummary()
    started = time.monotonic()

    cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)

    stmt = select(Company).where(
        Company.website.is_(None),
        or_(
            Company.website_resolved_at.is_(None),
            Company.website_resolved_at < cutoff,
        ),
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    companies = result.scalars().all()

    for company in companies:
        if (
            max_runtime_minutes is not None
            and time.monotonic() - started >= max_runtime_minutes * 60
        ):
            summary.stopped_early = True
            logger.info(
                "resolve: %.0f-minute budget reached after %d companies — "
                "stopping cleanly (%d left for the next run)",
                max_runtime_minutes,
                summary.companies_seen,
                len(companies) - summary.companies_seen,
            )
            break

        summary.companies_seen += 1

        slug_base = slugify(company.name)
        # Protect against companies whose slugified name is empty (edge case).
        if not slug_base:
            logger.warning("Company %s has empty slugified name — skipping resolve", company.id)
            summary.errors += 1
            continue

        try:
            resolved = await resolve_homepage(
                client,
                slug_base=slug_base,
                company_name=company.name,
                rejected_urls=company.rejected_urls or (),
            )
        except Exception:
            logger.exception("Unexpected error resolving homepage for %s", company.name)
            summary.errors += 1
            continue

        now = datetime.now(tz=UTC)

        if resolved is not None:
            if company.website != resolved:
                company.website = resolved
                summary.websites_resolved += 1
            else:
                # website was already set to this URL
                summary.websites_unchanged += 1
        else:
            summary.no_match += 1
            if client.ddg_blocked:
                summary.no_match_ddg_blocked += 1
            # website stays as-is; we record the attempt timestamp either way.

        company.website_resolved_at = now
        session.add(company)
        try:
            await session.commit()
        except StaleDataError:
            # The row was deleted out from under us mid-run — almost always a
            # concurrent dedup-companies merge folding this company into another.
            # Nothing to resolve; roll back and move on instead of crashing.
            await session.rollback()
            logger.warning(
                "Company %s disappeared mid-resolve (likely a concurrent merge)"
                " — skipping.",
                company.id,
            )
            summary.errors += 1
            continue

    return summary
