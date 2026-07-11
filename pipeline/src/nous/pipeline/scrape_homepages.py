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
4. Take the top ``max_extra_pages`` (default 5; was 3 until W-F) unique URLs.
5. Fetch and persist each one.

So a typical company contributes 1 (homepage) + ≤5 (discovered) = ≤6
real-existing pages. No 404 noise. The default was raised 3 → 5 for W-F so
content-rich sites feed the dedicated long-description call more source
text; the judge call still truncates at the shared 32k ceiling, so only the
describe call (which runs on catalog-worthy companies only) sees the extra
input. Cost: at most 2 extra same-host fetches per company per refetch
cycle (serialized at 1 req/s) and a few KB more stored text per company.

JS-shell fallback: if the httpx-fetched HTML extracts to less than
``_BROWSER_FALLBACK_TEXT_THRESHOLD`` chars of visible text (a sign that the
page is a React/Next.js shell waiting for hydration), the optional
``browser_client`` is used to re-fetch via headless Chromium, replacing the
stored content with the JS-rendered DOM. This recovers sites like
anspect-technologies.com that have no static body content at all.

Storage semantics: ``raw_pages.content`` holds the *extracted visible text*
of the page (capped at ``_MAX_STORED_CHARS``), not the raw HTML. Every
downstream consumer (enrich-companies, extract-funding-website) runs
``extract_visible_text`` over the content anyway — which is a no-op on
already-extracted text — and link discovery happens in memory here before
persisting. Raw HTML at backlog scale (~9k pages × ~200KB) would exceed
Supabase's 500MB free tier ~4×; extracted text fits in tens of MB. The
trade-off is that re-extraction without re-scraping is no longer possible;
the 90-day refetch window re-fetches from the live site instead.

Commit cadence: one commit per company so a mid-run crash leaves a clean
state, and so a ``max_runtime_minutes`` budget can stop the loop at any
company boundary — the next run's selection resumes via last_scrape_attempt_at
/ raw_pages timestamps.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.db.upsert import upsert_raw_page
from nous.sources.favicon import fetch_logo_url
from nous.sources.headless_browser import HeadlessBrowserClient
from nous.sources.homepage import FetchResult, HomepageClient, RobotsBlockedError
from nous.util.ssrf import BlockedAddressError
from nous.util.text import extract_visible_text

# Below this many chars of extracted visible text, the page is almost
# certainly a JS-only SPA shell — trigger the headless-browser fallback if
# one is configured.
_BROWSER_FALLBACK_TEXT_THRESHOLD: int = 200

# Per-page cap on stored extracted text. Enrichment truncates the multi-page
# concatenation anyway (32k for the judge call, 48k for the describe call);
# 50k per page is generous headroom while bounding pathological pages
# (infinite-scroll blogs, generated content).
_MAX_STORED_CHARS: int = 50_000

# How many companies to scrape over the network at once. Scraping is
# network-bound (homepage + up to 5 internal pages per company, plus an
# optional headless-Chromium render for JS-only shells). Distinct companies
# live on distinct domains, so a batch fetches concurrently while the clients'
# per-domain locks still serialize same-domain requests at 1 req/sec — within a
# single company its own pages are same-host and therefore stay serial.
_DEFAULT_CONCURRENCY: int = 6

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
    # Count of pages where the static httpx HTML was too thin to enrich and
    # the headless-browser fallback produced richer content that we stored
    # instead. Useful signal for "how much JS-shell content are we rescuing?"
    pages_via_browser_fallback: int = 0
    # Count of companies for which a logo/favicon URL was validated and stored
    # on this run. Best-effort: a homepage that yields no usable image simply
    # doesn't increment this (and never fails the scrape).
    logos_found: int = 0
    # True when the max_runtime_minutes budget stopped the loop before the
    # selection was drained. The remaining companies stay eligible next run.
    stopped_early: bool = False


async def _fetch_one(
    client: HomepageClient, url: str
) -> FetchResult | str | None:
    """Fetch a single URL. Returns FetchResult on success, the string
    "robots" on RobotsBlockedError, or None on HTTP/network/SSRF-block failure.

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
    except BlockedAddressError as exc:
        # SSRF guard rejected this URL (internal/unresolvable host, or a
        # redirect to one). Treat it like an unreachable site, not an
        # unexpected error.
        logger.info("scrape: SSRF guard blocked %s: %s", url, exc)
        return None


async def _resolve_logo_url(
    client: HomepageClient, homepage_html: str, homepage_url: str
) -> str | None:
    """Best-effort logo URL for a fetched homepage, or ``None``.

    Reuses the HomepageClient's own SSRF-guarded httpx client to validate the
    favicon/apple-touch-icon candidate (see :mod:`nous.sources.favicon`). The
    favicon is a small same-host asset, so this adds at most one lightweight
    HEAD/GET per company per refetch cycle.

    Wrapped so logo discovery can never break a scrape: any failure (including
    the client not being open, e.g. a test double) yields ``None`` and a log
    line, and the page persistence proceeds unaffected.
    """
    try:
        guarded_client, _ = client._assert_open()
    except RuntimeError:
        # A test/mocked HomepageClient may not run the real context manager;
        # logo discovery is optional, so skip silently rather than error.
        return None
    try:
        return await fetch_logo_url(guarded_client, homepage_html, homepage_url)
    except Exception:
        logger.info(
            "scrape: logo discovery failed for %s (continuing without logo)",
            homepage_url,
            exc_info=True,
        )
        return None


async def _persist_fetched_page(
    session: AsyncSession,
    company_id: object,
    *,
    url: str,
    content: str,
) -> None:
    """Extract visible text from the fetched HTML, sanitize, and upsert.

    Stores extracted text (see module docstring) capped at _MAX_STORED_CHARS.
    Postgres TEXT rejects NUL bytes; some sites serve binary disguised as
    text/html, so strip them defensively.
    """
    text = extract_visible_text(content)[:_MAX_STORED_CHARS]
    sanitized_content = text.replace("\x00", "")
    await upsert_raw_page(
        session,
        company_id=company_id,  # type: ignore[arg-type]
        url=url,
        content=sanitized_content,
    )


async def _resolve_content_with_fallback(
    static_result: FetchResult,
    browser_client: HeadlessBrowserClient | None,
) -> tuple[str, bool]:
    """Return ``(content, used_browser_fallback)`` for a fetched page.

    If the static HTML extracts to too little visible text AND a browser
    client is configured, fetch the same URL via headless Chromium and
    return that instead. Falls back to the static content when the browser
    path fails or returns no improvement.
    """
    if browser_client is None:
        return static_result.content, False

    static_text_len = len(extract_visible_text(static_result.content))
    if static_text_len >= _BROWSER_FALLBACK_TEXT_THRESHOLD:
        return static_result.content, False

    logger.info(
        "scrape: static HTML too thin (%d chars) for %s — trying browser fallback",
        static_text_len,
        static_result.url,
    )
    rendered = await browser_client.fetch_rendered_html(static_result.url)
    if rendered is None:
        return static_result.content, False
    rendered_text_len = len(extract_visible_text(rendered))
    if rendered_text_len <= static_text_len:
        # Browser didn't actually help (still empty, or worse). Keep static.
        logger.info(
            "scrape: browser fallback for %s returned %d chars (≤ static %d) — keeping static",
            static_result.url,
            rendered_text_len,
            static_text_len,
        )
        return static_result.content, False
    logger.info(
        "scrape: browser fallback for %s recovered %d chars (was %d static)",
        static_result.url,
        rendered_text_len,
        static_text_len,
    )
    return rendered, True


class _ScrapeOutcome(NamedTuple):
    """HTTP-only result of scraping one company (no DB access).

    ``homepage_status`` is one of:
      - "robots": homepage disallowed by robots.txt (site alive, not a failure)
      - "dead":   homepage fetch failed entirely (the dead-site signal)
      - "ok":     homepage fetched; ``pages`` holds (url, content) to persist
    ``pages`` lists the homepage first, then any discovered sub-pages, in fetch
    order. The counters cover sub-pages only; homepage-level robots/dead are
    conveyed via ``homepage_status``.
    ``logo_url`` is the validated external favicon/apple-touch-icon URL on the
    company's own domain (``None`` when no usable image was found); set on the
    company during the sequential persist phase.
    """

    homepage_status: str
    pages: list[tuple[str, str]]
    sub_skipped_robots: int
    sub_failed: int
    pages_via_browser_fallback: int
    logo_url: str | None


async def _scrape_one(
    client: HomepageClient,
    browser_client: HeadlessBrowserClient | None,
    website: str,
    *,
    max_extra_pages: int,
) -> _ScrapeOutcome:
    """Fetch a company's homepage + relevant sub-pages over the network.

    No DB access, so a batch of companies can run concurrently against the
    shared clients — their per-domain locks keep 1 req/sec/domain, and a single
    company's own (same-host) pages stay serial within this coroutine.
    """
    homepage_url = urljoin(website, "/")
    homepage = await _fetch_one(client, homepage_url)
    if homepage == "robots":
        return _ScrapeOutcome("robots", [], 0, 0, 0, None)
    if homepage is None:
        return _ScrapeOutcome("dead", [], 0, 0, 0, None)
    assert isinstance(homepage, FetchResult)

    pages: list[tuple[str, str]] = []
    browser_fallbacks = 0
    sub_skipped_robots = 0
    sub_failed = 0

    homepage_content, used_browser = await _resolve_content_with_fallback(
        homepage, browser_client
    )
    if used_browser:
        browser_fallbacks += 1
    pages.append((homepage.url, homepage_content))

    # Best-effort logo discovery from the homepage <head>. Parse the HTML we
    # actually fetched (the JS-rendered DOM when the browser fallback fired —
    # it carries the same icon links), validate the candidate is a real image,
    # and carry the external URL on the outcome. Never fail the scrape over it.
    logo_url = await _resolve_logo_url(client, homepage_content, homepage.url)

    # Discover relevant subpages from whichever HTML we ended up storing (the
    # JS-rendered version contains the real <a> tags too).
    discovered = _extract_relevant_links(
        homepage_content,
        base_url=homepage.url,
        max_links=max_extra_pages,
    )
    # Sub-pages are same-host as the homepage, so the per-domain lock serializes
    # them within this coroutine — intentional (don't hammer one site).
    for sub_url in discovered:
        sub = await _fetch_one(client, sub_url)
        if sub == "robots":
            sub_skipped_robots += 1
            continue
        if sub is None:
            sub_failed += 1
            continue
        assert isinstance(sub, FetchResult)
        sub_content, sub_used_browser = await _resolve_content_with_fallback(
            sub, browser_client
        )
        if sub_used_browser:
            browser_fallbacks += 1
        pages.append((sub.url, sub_content))

    return _ScrapeOutcome(
        "ok", pages, sub_skipped_robots, sub_failed, browser_fallbacks, logo_url
    )


async def run_scrape_homepages(
    session: AsyncSession,
    client: HomepageClient,
    *,
    refetch_after_days: int = 90,
    failure_backoff_days: int = 30,
    limit: int | None = None,
    max_extra_pages: int = 5,
    browser_client: HeadlessBrowserClient | None = None,
    max_runtime_minutes: float | None = None,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> ScrapeSummary:
    """For each company with website set AND no recent raw_pages:
    1. Fetch the homepage via the static httpx client.
    2. If the page extracts to too few chars of visible text and a
       ``browser_client`` was supplied, re-fetch via headless Chromium and
       store the JS-rendered DOM instead.
    3. Discover up to ``max_extra_pages`` relevant internal links from the
       fetched HTML (about / team / product / etc., scored against link text
       + path).
    4. Fetch and persist each.

    A company is eligible when:
    - company.website IS NOT NULL, AND
    - it has at least one raw_page OR last_scrape_attempt_at is NULL/older
      than ``failure_backoff_days``, AND
    - it has no raw_pages OR all of its raw_pages.fetched_at <
      (now - ``refetch_after_days``).

    ``max_runtime_minutes`` is a clean-exit wall-clock budget: the loop stops
    at the next *batch* boundary once exceeded. Combined with per-company
    commits this makes the stage resumable across bounded CI runs.

    ``concurrency`` controls how many companies are fetched over the network at
    once. Only the HTTP work is parallelized; persistence stays strictly
    sequential on the single passed-in session (one connection, one commit per
    company), so there is no added DB concurrency and the existing
    crash-safety/idempotency is unchanged.
    """
    summary = ScrapeSummary()
    started = time.monotonic()
    deadline = (
        started + max_runtime_minutes * 60 if max_runtime_minutes is not None else None
    )

    cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)
    failure_cutoff = datetime.now(tz=UTC) - timedelta(days=failure_backoff_days)

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
        .where(Company.website.is_not(None))
        .where(Company.exclusion_reason.is_(None))
        .where(
            (latest_fetch_subq.c.latest_fetched_at.is_(None))
            | (latest_fetch_subq.c.latest_fetched_at < cutoff)
        )
        # Failure back-off: if a company has zero pages on record and was
        # attempted recently, suppress retry until the back-off elapses.
        # Companies WITH at least one page stay eligible whenever the
        # refetch_after_days predicate above fires.
        .where(
            (latest_fetch_subq.c.latest_fetched_at.is_not(None))
            | (Company.last_scrape_attempt_at.is_(None))
            | (Company.last_scrape_attempt_at < failure_cutoff)
        )
        # Prominence-first: when --limit only admits a slice of the backlog,
        # scrape the highest-raise companies first so marquee names get pages
        # (and thus enrichment) ahead of the long tail. latest_round_amount is
        # the denormalized "most recent round" column on companies; NULLS LAST
        # keeps amount-less companies behind funded ones. funding_round_count
        # breaks ties, and id makes successive bounded runs deterministic.
        # Eligibility (the WHERE clauses above) is unchanged.
        .order_by(
            Company.latest_round_amount.desc().nulls_last(),
            Company.funding_round_count.desc(),
            Company.id,
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    companies = list(result.scalars().all())

    batch_size = max(1, concurrency)
    for start in range(0, len(companies), batch_size):
        if deadline is not None and time.monotonic() >= deadline:
            summary.stopped_early = True
            logger.info(
                "scrape: %.0f-minute budget reached after %d companies — "
                "stopping cleanly (%d left for the next run)",
                max_runtime_minutes,
                summary.companies_seen,
                len(companies) - summary.companies_seen,
            )
            break

        batch = companies[start : start + batch_size]
        # Phase 1: fetch every company's pages concurrently (network only).
        # return_exceptions keeps one freak error from sinking the whole batch.
        outcomes = await asyncio.gather(
            *(
                _scrape_one(
                    client,
                    browser_client,
                    c.website,  # type: ignore[arg-type]  # query guarantees IS NOT NULL
                    max_extra_pages=max_extra_pages,
                )
                for c in batch
            ),
            return_exceptions=True,
        )

        # Phase 2: apply results sequentially on the single session — one commit
        # per company, stamping last_scrape_attempt_at in a finally so the
        # back-off window applies even when persistence fails.
        for company, raw_outcome in zip(batch, outcomes, strict=True):
            summary.companies_seen += 1

            if isinstance(raw_outcome, BaseException):
                # Unexpected fetch error — treat as a dead homepage for this run
                # (stamp + back-off below); never crash the whole batch.
                logger.error(
                    "scrape: unexpected error scraping %s: %r",
                    company.website,
                    raw_outcome,
                )
                outcome = _ScrapeOutcome("dead", [], 0, 0, 0, None)
            else:
                outcome = raw_outcome

            try:
                if outcome.homepage_status == "robots":
                    # robots.txt block ⇒ the site is alive, just disallowing us.
                    # Leave consecutive_scrape_failures untouched (not dead).
                    summary.pages_skipped_robots += 1
                    summary.companies_with_no_pages += 1
                elif outcome.homepage_status == "dead":
                    # Total fetch failure ⇒ the dead-site signal. Re-runs add one
                    # per failed cycle; persisted by the finally-block commit.
                    company.consecutive_scrape_failures += 1
                    summary.pages_failed += 1
                    summary.companies_with_no_pages += 1
                else:  # "ok"
                    # Homepage reachable ⇒ reset the dead-site counter.
                    company.consecutive_scrape_failures = 0
                    # Refresh the logo on the same cadence as the pages: a
                    # company only reaches "ok" here when it was due for a
                    # (re)scrape, so adopt any freshly-validated favicon URL.
                    # Only overwrite when we actually found one — never clobber
                    # an existing logo with NULL on a transient miss. Idempotent:
                    # the same homepage yields the same candidate each run.
                    if outcome.logo_url is not None:
                        company.logo_url = outcome.logo_url
                        summary.logos_found += 1
                    summary.pages_skipped_robots += outcome.sub_skipped_robots
                    summary.pages_failed += outcome.sub_failed
                    summary.pages_via_browser_fallback += (
                        outcome.pages_via_browser_fallback
                    )
                    for url, content in outcome.pages:
                        await _persist_fetched_page(
                            session, company.id, url=url, content=content
                        )
                        summary.pages_fetched += 1
            finally:
                # Stamp the attempt timestamp on every iteration so the back-off
                # window applies even when a persist raised (e.g. a DB hiccup).
                # If the in-flight transaction is poisoned, roll back, then
                # commit just the timestamp update.
                try:
                    company.last_scrape_attempt_at = datetime.now(tz=UTC)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    fresh = await session.get(Company, company.id)
                    if fresh is not None:
                        fresh.last_scrape_attempt_at = datetime.now(tz=UTC)
                        await session.commit()

    return summary
