"""scrape-homepages pipeline stage.

For each company with a website and no recent raw_pages: fetch the homepage,
parse its HTML for relevant internal links (about / team / product / etc.),
then fetch the top N of those. Stores everything in raw_pages.

Earlier versions hardcoded 7 candidate paths (/, /about, /about-us, /product,
/products, /company, /team) — a guess that produced ~80% 404s on paths sites
never had. The discovery approach below uses what the site actually links to:

1. Fetch the homepage ``/``.
2. Parse <a href> tags via selectolax. Keep only same-host internal links.
3. Score each by how many "about/team/product/company/story/..." keywords
   appear in the URL path or anchor text. Higher = more interesting.
4. Take the top ``max_extra_pages`` (default 3) unique URLs.
5. Fetch and persist each one.

So a typical company contributes 1 (homepage) + ≤3 (discovered) = ≤4
real-existing pages. No 404 noise.

Commit cadence: one commit per company so a mid-run crash leaves a clean state.

Known limitation (~21% of companies): sites behind Cloudflare/WAF that
fingerprint at the TLS/IP layer (e.g. adquick.com) reject the GitHub Actions
runner outright with 403, regardless of User-Agent. Recovering those would
require a headless browser (Playwright) or residential proxy — left to M5.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.db.upsert import upsert_raw_page
from nous.sources.homepage import FetchResult, HomepageClient, RobotsBlockedError

logger = logging.getLogger(__name__)

# Words that, when present in a link's URL path or visible text, suggest the
# page is informative for "what does this company do" enrichment. Tuned for
# B2B software / startup marketing sites.
_RELEVANCE_KEYWORDS: tuple[str, ...] = (
    "about",
    "team",
    "company",
    "story",
    "mission",
    "product",
    "platform",
    "solution",
    "what-we-do",
    "how-it-works",
    "manifesto",
)

# Path suffixes / segments to always reject — non-prose, marketing-noise, or
# legal boilerplate that adds nothing useful.
_REJECT_PATH_PATTERNS = re.compile(
    r"(?:\.(?:pdf|png|jpg|jpeg|gif|svg|webp|css|js|ico|xml|zip|mp4|webm)$|"
    r"/(?:privacy|terms|legal|cookie|gdpr|login|signin|signup|register|"
    r"contact|support|help|press|blog|news|careers|jobs|pricing|docs?|api)(?:/|$))",
    re.IGNORECASE,
)


def _extract_relevant_links(
    html: str, base_url: str, *, max_links: int = 3
) -> list[str]:
    """Parse HTML and return up to ``max_links`` same-host URLs most likely to
    describe what the company does.

    Scoring is simple keyword counting against the URL path + anchor text.
    Ties are broken by anchor text length (favor descriptive links). Empty
    or asset URLs are dropped before scoring.
    """
    tree = HTMLParser(html)
    base_host = urlparse(base_url).netloc.lower()
    if not base_host:
        return []

    seen: set[str] = set()
    scored: list[tuple[int, int, str]] = []  # (-score, -text_len, url) for sort

    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() != base_host:
            continue  # off-site link
        # Strip fragment + dedupe; treat trailing slash variants as the same.
        canonical = parsed._replace(fragment="").geturl().rstrip("/")
        if canonical == base_url.rstrip("/"):
            continue  # the homepage itself
        if _REJECT_PATH_PATTERNS.search(parsed.path):
            continue
        if canonical in seen:
            continue
        seen.add(canonical)

        text = (node.text(strip=True) or "").lower()
        path_lower = parsed.path.lower()
        score = sum(
            1
            for kw in _RELEVANCE_KEYWORDS
            if kw in path_lower or kw in text
        )
        if score == 0:
            continue
        scored.append((-score, -len(text), canonical))

    scored.sort()
    return [url for (_, _, url) in scored[:max_links]]


class ScrapeSummary(BaseModel):
    companies_seen: int = 0
    pages_fetched: int = 0
    pages_skipped_robots: int = 0
    pages_failed: int = 0
    companies_with_no_pages: int = 0


async def _fetch_one(
    client: HomepageClient, url: str
) -> FetchResult | str | None:
    """Fetch a single URL. Returns FetchResult on success, the string
    "robots" on RobotsBlockedError, or None on HTTP/network failure.

    The tagged-string return is deliberate: callers need to distinguish
    "site explicitly blocks us" from "site is dead/unreachable" for metric
    bookkeeping, and exceptions across an await fold awkwardly otherwise.
    """
    try:
        return await client.fetch(url)
    except RobotsBlockedError as exc:
        logger.info("scrape: robots.txt blocked %s (%s)", url, exc)
        return "robots"
    except httpx.HTTPStatusError as exc:
        logger.info(
            "scrape: HTTP %d on %s — likely WAF/Cloudflare block",
            exc.response.status_code,
            url,
        )
        return None
    except httpx.RequestError as exc:
        logger.info(
            "scrape: network error on %s: %s: %s",
            url,
            type(exc).__name__,
            exc,
        )
        return None


async def _persist_fetched_page(
    session: AsyncSession, company_id: object, result: FetchResult
) -> None:
    """Sanitize + upsert the page. Postgres TEXT rejects NUL bytes; some sites
    serve binary disguised as text/html, so strip them defensively.
    """
    sanitized_content = result.content.replace("\x00", "")
    await upsert_raw_page(
        session,
        company_id=company_id,  # type: ignore[arg-type]
        url=result.url,  # final URL after redirects
        content=sanitized_content,
    )


async def run_scrape_homepages(
    session: AsyncSession,
    client: HomepageClient,
    *,
    refetch_after_days: int = 90,
    limit: int | None = None,
    max_extra_pages: int = 3,
) -> ScrapeSummary:
    """For each company with website set AND no recent raw_pages:
    1. Fetch the homepage.
    2. Discover up to ``max_extra_pages`` relevant internal links from it
       (about / team / product / etc., scored against page link text + path).
    3. Fetch and persist each.

    A company is eligible when:
    - company.website IS NOT NULL, AND
    - it has no raw_pages OR all of its raw_pages.fetched_at < (now - refetch_after_days).
    """
    summary = ScrapeSummary()

    cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)

    # Find companies with a website that have no recent raw_pages.
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
        homepage_url = urljoin(website, "/")

        # Step 1: fetch the homepage.
        homepage = await _fetch_one(client, homepage_url)
        if homepage == "robots":
            summary.pages_skipped_robots += 1
            summary.companies_with_no_pages += 1
            await session.commit()
            continue
        if homepage is None:
            summary.pages_failed += 1
            summary.companies_with_no_pages += 1
            await session.commit()
            continue
        assert isinstance(homepage, FetchResult)

        await _persist_fetched_page(session, company.id, homepage)
        summary.pages_fetched += 1

        # Step 2: discover relevant subpages from the homepage HTML.
        # Use the post-redirect URL as the base so relative links resolve correctly.
        discovered = _extract_relevant_links(
            homepage.content,
            base_url=homepage.url,
            max_links=max_extra_pages,
        )

        # Step 3: fetch each discovered link.
        for sub_url in discovered:
            sub = await _fetch_one(client, sub_url)
            if sub == "robots":
                summary.pages_skipped_robots += 1
                continue
            if sub is None:
                summary.pages_failed += 1
                continue
            assert isinstance(sub, FetchResult)
            await _persist_fetched_page(session, company.id, sub)
            summary.pages_fetched += 1

        await session.commit()

    return summary
