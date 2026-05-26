"""resolve-homepages pipeline stage.

For each company that has no website (or whose website_resolved_at is stale),
try CANDIDATE_TLDS to find a live homepage and record the result.

Commit cadence: one commit per company so a mid-run crash leaves a clean state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.sources.homepage import HomepageClient, resolve_homepage
from nous.util.slugify import slugify

logger = logging.getLogger(__name__)


class ResolveSummary(BaseModel):
    companies_seen: int = 0
    websites_resolved: int = 0
    websites_unchanged: int = 0
    no_match: int = 0
    errors: int = 0


async def run_resolve_homepages(
    session: AsyncSession,
    client: HomepageClient,
    *,
    refetch_after_days: int = 90,
    limit: int | None = None,
) -> ResolveSummary:
    """For each company where website is NULL or website_resolved_at is stale,
    attempt to resolve a homepage via common TLDs.

    On a non-None result, update company.website + company.website_resolved_at.
    On None, still set website_resolved_at (so we don't retry every run);
    leave website as-is.
    """
    summary = ResolveSummary()

    cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)

    stmt = select(Company).where(
        or_(
            Company.website.is_(None),
            Company.website_resolved_at.is_(None),
            Company.website_resolved_at < cutoff,
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    companies = result.scalars().all()

    for company in companies:
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
            # website stays as-is; we record the attempt timestamp either way.

        company.website_resolved_at = now
        session.add(company)
        await session.commit()

    return summary
