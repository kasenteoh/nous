"""Targeted one-off: re-resolve (or hard-set) a single company's homepage.

Companies discovered name-only via VC portfolios and resolved BEFORE the
curl_cffi Cloudflare bypass (PR #132, 2026-07-10) can be stuck website-less:
plain httpx got a 403 challenge, every candidate was rejected, and the 90-day
``website_resolved_at`` window keeps them out of re-resolution for months.
Perplexity is the flagship case. This command re-runs the (now stronger)
resolver for one company and persists the result, so the full downstream chain
(scrape → render → enrich → describe → embed) can be proven end-to-end without
waiting for the window to recycle.

``--set-url`` bypasses resolution and hard-sets the website — a fallback for
sites whose Cloudflare challenge also blocks the resolver's curl_cffi fetch
from a datacenter (Actions) IP. Dispatched via ops.yml against prod.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.sources.homepage import HomepageClient, resolve_homepage
from nous.util.slugify import slugify


class ReresolveResult(BaseModel):
    """Outcome of a single-company re-resolution."""

    found: bool
    slug: str
    previous_website: str | None = None
    resolved_website: str | None = None
    method: str | None = None  # "resolver" | "set-url" | None (company missing)
    changed: bool = False


async def run_reresolve_company(
    session: AsyncSession,
    client: HomepageClient,
    *,
    slug: str,
    set_url: str | None = None,
) -> ReresolveResult:
    """Re-resolve (or hard-set via ``set_url``) one company's homepage and
    persist ``website`` + ``website_resolved_at``. Read-then-write; commits."""
    company = (
        await session.execute(select(Company).where(Company.slug == slug))
    ).scalar_one_or_none()
    if company is None:
        return ReresolveResult(found=False, slug=slug)

    previous = company.website
    if set_url is not None:
        resolved: str | None = set_url
        method = "set-url"
    else:
        slug_base = slugify(company.name)
        resolved = (
            await resolve_homepage(
                client,
                slug_base=slug_base,
                company_name=company.name,
                rejected_urls=company.rejected_urls or (),
            )
            if slug_base
            else None
        )
        method = "resolver"

    # Always stamp the attempt (mirrors run_resolve_homepages: a miss still
    # updates website_resolved_at so the standing window governs the next try).
    company.website_resolved_at = datetime.now(UTC)
    if resolved is not None:
        company.website = resolved
    await session.commit()

    return ReresolveResult(
        found=True,
        slug=slug,
        previous_website=previous,
        resolved_website=resolved,
        method=method,
        changed=resolved is not None and resolved != previous,
    )
