"""scrape-homepages pipeline stage.

For each company with a website set and no recent raw_pages, fetch
CANDIDATE_PATHS and cache the HTML in raw_pages.

Commit cadence: one commit per company so a mid-run crash leaves a clean state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.db.upsert import upsert_raw_page
from nous.sources.homepage import CANDIDATE_PATHS, HomepageClient, RobotsBlockedError

logger = logging.getLogger(__name__)


class ScrapeSummary(BaseModel):
    companies_seen: int = 0
    pages_fetched: int = 0
    pages_skipped_robots: int = 0
    pages_failed: int = 0
    companies_with_no_pages: int = 0


async def run_scrape_homepages(
    session: AsyncSession,
    client: HomepageClient,
    *,
    refetch_after_days: int = 90,
    limit: int | None = None,
    max_pages_per_company: int = 4,
) -> ScrapeSummary:
    """For each company with website set AND no recent raw_pages:
    fetch CANDIDATE_PATHS and store HTML.

    A company is eligible when:
    - company.website IS NOT NULL, AND
    - it has no raw_pages OR all of its raw_pages.fetched_at < (now - refetch_after_days).
    """
    summary = ScrapeSummary()

    cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)

    # Find companies with a website that have no recent raw_pages.
    # "Recent" means at least one raw_page with fetched_at >= cutoff.
    # We use a subquery: companies whose max(fetched_at) is NULL or old.
    latest_fetch_subq = (
        select(
            RawPage.company_id,
            func.max(RawPage.fetched_at).label("latest_fetched_at"),
        )
        .group_by(RawPage.company_id)
        .subquery()
    )

    stmt = (
        select(Company)
        .outerjoin(
            latest_fetch_subq,
            Company.id == latest_fetch_subq.c.company_id,
        )
        .where(
            Company.website.is_not(None),
        )
        .where(
            # No raw_pages at all, OR latest fetch is stale.
            (latest_fetch_subq.c.latest_fetched_at.is_(None))
            | (latest_fetch_subq.c.latest_fetched_at < cutoff)
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    companies = result.scalars().all()

    for company in companies:
        summary.companies_seen += 1

        website: str = company.website  # type: ignore[assignment]  # already checked IS NOT NULL
        pages_this_company = 0

        for path in CANDIDATE_PATHS:
            if pages_this_company >= max_pages_per_company:
                break

            url = urljoin(website, path)
            try:
                fetch_result = await client.fetch(url)
            except RobotsBlockedError:
                logger.debug("robots.txt blocked %s", url)
                summary.pages_skipped_robots += 1
                continue
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.debug("Failed to fetch %s: %s", url, exc)
                summary.pages_failed += 1
                continue

            # Postgres TEXT columns reject NUL (0x00) bytes. Some sites serve
            # binary disguised as text/html, or include stray NULs in error
            # pages. Strip them — they're never legitimate in HTML.
            sanitized_content = fetch_result.content.replace("\x00", "")
            await upsert_raw_page(
                session,
                company_id=company.id,
                url=fetch_result.url,  # use final URL after redirects
                content=sanitized_content,
            )
            pages_this_company += 1
            summary.pages_fetched += 1

        if pages_this_company == 0:
            summary.companies_with_no_pages += 1

        await session.commit()

    return summary
