"""estimate-employees pipeline stage.

For each company with no employee count (or a stale check), probe public
sources in priority order — The Org → GrowJo → careers-page job count →
GitHub org → Wellfound — and record the first non-null ``(min, max)`` range
plus its source label. The source is stored so the company page can attribute
the number (spec §3.4: every rendered fact has a recorded source).

Wellfound is tried last because it is mostly Cloudflare-blocked, burning a
wasted request per company when placed earlier in the chain.

Mirrors resolve-homepages: one commit per company so a mid-run crash leaves a
clean state, ``employee_count_checked_at`` stamped on every attempt (success,
no-data, or error) for refetch back-off, and ``StaleDataError`` tolerated when
a concurrent dedup-companies merge deletes the row mid-run.
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
from nous.sources import careers_jobs, github_org, growjo, theorg, wellfound
from nous.sources.homepage import HomepageClient

logger = logging.getLogger(__name__)


class EstimateEmployeesSummary(BaseModel):
    companies_seen: int = 0
    updated: int = 0
    unchanged: int = 0
    no_data: int = 0
    errors: int = 0
    # True when the max_runtime_minutes budget stopped the loop before the
    # selection was drained. The remaining companies stay eligible next run.
    stopped_early: bool = False


async def run_estimate_employees(
    session: AsyncSession,
    client: HomepageClient,
    github_token: str,
    *,
    refetch_after_days: int = 90,
    limit: int | None = None,
    max_runtime_minutes: float | None = None,
) -> EstimateEmployeesSummary:
    """Fill employee_count_{min,max,source} for eligible companies.

    Eligible = never checked, or last checked before the cutoff. The back-off
    deliberately applies to no-data companies too — most small startups are on
    none of the five sources, and re-probing them every run would let the same
    no-data block occupy the whole per-run limit forever. On a hit, update the
    range + source; on no data, leave the count as-is. Either way stamp
    employee_count_checked_at so we don't re-probe every run.

    ``max_runtime_minutes`` is a clean-exit wall-clock budget: the loop stops
    at the next company boundary once exceeded.
    """
    summary = EstimateEmployeesSummary()
    started = time.monotonic()

    cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)

    stmt = select(Company).where(
        Company.exclusion_reason.is_(None),
        or_(
            Company.employee_count_checked_at.is_(None),
            Company.employee_count_checked_at < cutoff,
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
                "estimate-employees: %.0f-minute budget reached after %d "
                "companies — stopping cleanly (%d left for the next run)",
                max_runtime_minutes,
                summary.companies_seen,
                len(companies) - summary.companies_seen,
            )
            break

        summary.companies_seen += 1
        now = datetime.now(tz=UTC)

        probe: tuple[tuple[int, int], str] | None = None
        try:
            probe = await _probe_employee_count(client, company, github_token)
        except Exception:  # noqa: BLE001 — one company's failure shouldn't sink the run
            logger.exception("estimate_employees: unexpected error for %s", company.name)
            summary.errors += 1
        else:
            if probe is not None:
                (new_min, new_max), source = probe
                current = (company.employee_count_min, company.employee_count_max)
                if current != (new_min, new_max):
                    company.employee_count_min = new_min
                    company.employee_count_max = new_max
                    company.employee_count_source = source
                    summary.updated += 1
                else:
                    summary.unchanged += 1
            else:
                summary.no_data += 1

        # Stamp the attempt on every path (hit, no-data, or error) for back-off.
        company.employee_count_checked_at = now
        session.add(company)
        try:
            await session.commit()
        except StaleDataError:
            # Row deleted mid-run — almost always a concurrent dedup-companies
            # merge folding this company into another. Roll back and move on.
            await session.rollback()
            logger.warning(
                "Company %s disappeared mid-estimate (likely a concurrent merge)"
                " — skipping.",
                company.id,
            )
            summary.errors += 1
            continue

    return summary


async def _probe_employee_count(
    client: HomepageClient, company: Company, github_token: str
) -> tuple[tuple[int, int], str] | None:
    """Try each source in priority order; return the first ``(range, source)``.

    Order: The Org → GrowJo → careers-page job count → GitHub → Wellfound.
    Wellfound is placed last because it is mostly Cloudflare-blocked and would
    burn a wasted request per company if tried earlier in the chain.
    """
    name = company.name

    org = await theorg.get_employee_range(client, name)
    if org is not None:
        return org, "theorg"

    gj = await growjo.get_employee_range(client, name)
    if gj is not None:
        return gj, "growjo"

    if company.website:
        careers = await careers_jobs.count_job_listings(client, company.website)
        if careers is not None:
            return careers, "careers_page"

    gh = await github_org.get_employee_range(client, name, github_token)
    if gh is not None:
        return gh, "github"

    # Wellfound last: mostly Cloudflare-blocked, so we keep it as a fallback
    # rather than the first probe.
    wf = await wellfound.get_employee_range(client, name)
    if wf is not None:
        return wf, "wellfound"

    return None
