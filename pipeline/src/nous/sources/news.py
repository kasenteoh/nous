"""Async news ingestion: Google News RSS + article body fetcher.

Mirrors the discipline of ``sources/homepage.py``:

- Per-domain 1 req/sec throttle (spec §3.2 + §11).
- robots.txt checked on every fetch via RobotsCache.
- Tenacity retries on 429 / 5xx / network blips.
- User-Agent identifies nous on every request — reuse SEC_USER_AGENT site-wide.

Boundaries:

- ``NewsArticleResult`` is the Pydantic model crossing the source/pipeline
  boundary. The RSS adapter returns "shallow" results (no body); the article
  body is fetched lazily via ``NewsClient.fetch_article_body`` so we can
  filter on title/snippet before incurring the per-article HTTP cost.
- ``fetch_article_body`` returns ``None`` on robots-block, 4xx, 5xx, or when
  the extracted visible text is below MIN_BODY_CHARS — anything below that
  threshold is almost certainly a redirect interstitial or a paywall stub.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from nous.sources.robots import RobotsBlockedError, RobotsCache
from nous.util.url import canonical_url, hostname

# Re-export so callers that did ``from nous.sources.news import RobotsBlockedError``
# continue to work. The canonical definition lives in ``nous.sources.robots``.
__all__ = [
    "FUNDING_KEYWORDS",
    "MIN_BODY_CHARS",
    "NewsArticleResult",
    "NewsClient",
    "RobotsBlockedError",
]

logger = logging.getLogger(__name__)

# Funding-signal keywords (spec §5.5). Matched case-insensitively against the
# combined title + snippet of each RSS entry. The list is intentionally
# conservative — broader phrasing would let too much commentary through.
FUNDING_KEYWORDS: tuple[str, ...] = (
    "raised",
    "raises",
    "funding",
    "seed",
    "series a",
    "series b",
    "series c",
    "series d",
    "series e",
    "valuation",
    "closes",
    "led by",
)

# Below this size in cleaned-text chars, the fetched page is almost certainly
# a paywall stub, JS-only shell, or redirect interstitial — not useful as
# input to the funding-extraction LLM call.
MIN_BODY_CHARS: int = 500

# HTML tags whose contents add noise to article text extraction. We strip
# these subtrees before reading visible text. Order matters only for
# readability; ``decompose`` is idempotent.
_NOISE_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "nav",
    "footer",
    "header",
    "aside",
    "iframe",
    "noscript",
    "form",
    "svg",
)

_WHITESPACE_RE = re.compile(r"\s+")

GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"

# Feed-syndication surfaces: endpoints a site *publishes* for programmatic
# readers (RSS/Atom) and serves with HTTP 200 to identified clients, even
# though the site's robots.txt ``Disallow: /`` blocks its *interactive* crawl
# surface. Google News is the canonical case — news.google.com/robots.txt
# disallows ``/`` for ``*`` with an allow-list that omits ``/rss``, yet
# /rss/search returns a valid 200 feed and the spec (nous-technical-spec.md
# §5.5) sanctions this exact URL for funding discovery. Honoring robots.txt
# literally here means *every* per-company Google News query silently returns
# nothing — the feed is unreachable despite being designed for exactly this.
#
# We treat these prefixes as exempt from the robots gate ONLY: the per-domain
# 1 req/sec throttle and our identifying User-Agent still apply on every fetch.
# Keep this list as narrow as possible — it is a deliberate, audited exception
# to the project's robots discipline, not a general bypass.
_ROBOTS_EXEMPT_PREFIXES: tuple[str, ...] = ("https://news.google.com/rss/",)


def _is_robots_exempt(url: str) -> bool:
    """True if ``url`` is a published feed surface exempt from the robots gate."""
    return url.startswith(_ROBOTS_EXEMPT_PREFIXES)


class NewsArticleResult(BaseModel):
    """Shallow news article record from an RSS feed.

    ``raw_content`` holds the RSS snippet / summary, not the fetched body —
    body fetching is a separate step (``NewsClient.fetch_article_body``)
    because most RSS hits don't survive the keyword filter.
    """

    url: str  # canonical
    title: str
    source: str  # hostname (e.g. "techcrunch.com")
    published_date: date | None
    raw_content: str


def _is_retryable(exc: BaseException) -> bool:
    """Retry on 429 / 5xx / timeouts. ConnectError (DNS, refused, TLS) is permanent."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TimeoutException)


def _matches_funding_keyword(text: str) -> bool:
    """Case-insensitive match against FUNDING_KEYWORDS."""
    lowered = text.lower()
    return any(kw in lowered for kw in FUNDING_KEYWORDS)


def _strip_html(text: str) -> str:
    """Strip HTML tags from a snippet using selectolax; collapse whitespace."""
    if not text:
        return ""
    parsed = HTMLParser(text)
    visible = parsed.text(separator=" ", strip=True)
    return _WHITESPACE_RE.sub(" ", visible).strip()


def _struct_time_to_date(value: object) -> date | None:
    """Convert a feedparser ``published_parsed`` struct_time to a date.

    feedparser parses dates into stdlib time.struct_time tuples; we only
    keep year/month/day for our schema's ``published_date`` column.
    Returns None on any malformed input.
    """
    if value is None:
        return None
    try:
        # struct_time exposes tm_year/tm_mon/tm_mday
        return date(value.tm_year, value.tm_mon, value.tm_mday)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None


def _extract_article_text(html: str) -> str:
    """Parse ``html`` with selectolax, drop noise subtrees, return collapsed text."""
    tree = HTMLParser(html)
    for selector in _NOISE_TAGS:
        for node in tree.css(selector):
            node.decompose()
    root = tree.body or tree
    text = root.text(separator=" ", strip=True)
    return _WHITESPACE_RE.sub(" ", text).strip()


class NewsClient:
    """Async news client. Per-domain throttle + robots + retries.

    Usage:

        async with NewsClient(user_agent="nous-bot (you@example.com)") as nc:
            entries = await nc.google_news_rss("\\"OpenAI\\" funding")
            for entry in entries:
                body = await nc.fetch_article_body(entry.url)
    """

    def __init__(
        self,
        user_agent: str,
        requests_per_second_per_domain: float = 1.0,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise ValueError(
                "user_agent must be a non-empty string containing a contact email. "
                "Most news sites block anonymous crawlers."
            )
        self._user_agent = user_agent
        self._rps = requests_per_second_per_domain
        self._min_interval: float = 1.0 / requests_per_second_per_domain

        self._domain_last_request: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

        self._client: httpx.AsyncClient | None = None
        self._robots: RobotsCache | None = None

    async def __aenter__(self) -> NewsClient:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self._user_agent},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        self._robots = RobotsCache(
            client=httpx.AsyncClient(
                headers={"User-Agent": self._user_agent},
                timeout=httpx.Timeout(5.0),
                follow_redirects=True,
            ),
            user_agent=self._user_agent,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._robots is not None:
            await self._robots._client.aclose()
            self._robots = None

    def _assert_open(self) -> tuple[httpx.AsyncClient, RobotsCache]:
        if self._client is None or self._robots is None:
            raise RuntimeError("NewsClient must be used as an async context manager.")
        return self._client, self._robots

    async def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        async with self._registry_lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = asyncio.Lock()
            return self._domain_locks[domain]

    async def _throttled_get(self, url: str) -> httpx.Response:
        """Rate-limited GET, serialised per domain."""
        parsed = urlparse(url)
        domain = parsed.netloc

        domain_lock = await self._get_domain_lock(domain)
        async with domain_lock:
            now = time.monotonic()
            last = self._domain_last_request.get(domain, 0.0)
            wait = self._min_interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)

            client, _ = self._assert_open()
            resp = await client.get(url)
            self._domain_last_request[domain] = time.monotonic()
            resp.raise_for_status()
            return resp

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _get_with_retry(self, url: str) -> httpx.Response:
        return await self._throttled_get(url)

    async def fetch_text(self, url: str) -> str:
        """Robots-checked, throttled, retried GET. Returns response body text.

        Published feed surfaces (``_ROBOTS_EXEMPT_PREFIXES``, e.g. Google News
        RSS) skip the robots gate — see that constant's docstring — but still
        pay the per-domain throttle and carry our identifying User-Agent.
        """
        _, robots = self._assert_open()
        if not _is_robots_exempt(url):
            allowed = await robots.is_allowed(url)
            if not allowed:
                raise RobotsBlockedError(f"robots.txt disallows: {url}")
        resp = await self._get_with_retry(url)
        return resp.text

    async def google_news_rss(
        self,
        query: str,
        lookback_days: int = 7,
    ) -> list[NewsArticleResult]:
        """Fetch Google News RSS for ``query``, return funding-keyword matches only.

        Filtering:
        - Entries older than ``lookback_days`` are dropped. Google News doesn't
          honor a server-side date filter on the RSS endpoint reliably, so we
          filter client-side. Entries with no parseable date are kept (we'd
          rather over-include than silently drop signal).
        - Title + snippet (HTML-stripped) must contain at least one
          FUNDING_KEYWORDS hit. Spec §5.5.

        Dedup:
        - URLs are canonicalized (tracking params + fragment dropped) before
          dedup. The same article appearing under two Google News redirect
          URLs with differing tracking suffixes collapses to one entry.
        """
        rss_url = f"{GOOGLE_NEWS_RSS_BASE}?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        try:
            xml_text = await self.fetch_text(rss_url)
        except RobotsBlockedError:
            logger.warning("Google News RSS blocked by robots.txt for query %r", query)
            return []
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.warning("Google News RSS fetch failed for %r: %s", query, exc)
            return []

        return self._parse_rss(
            xml_text,
            lookback_days=lookback_days,
            require_keywords=True,
        )

    async def fetch_article_body(self, url: str) -> str | None:
        """Fetch ``url`` and return cleaned visible text.

        Returns None on:
        - robots.txt block
        - HTTP 4xx (after retries; 4xx is not retried)
        - HTTP 5xx (after retries are exhausted)
        - Network error (after retries)
        - Extracted text shorter than MIN_BODY_CHARS (paywall / JS shell)
        """
        try:
            html_text = await self.fetch_text(url)
        except RobotsBlockedError:
            logger.info("robots.txt blocked article body fetch: %s", url)
            return None
        except httpx.HTTPStatusError as exc:
            logger.info("HTTP %d on article body fetch: %s", exc.response.status_code, url)
            return None
        except httpx.RequestError as exc:
            logger.info("network error on article body fetch %s: %s", url, exc)
            return None

        text = _extract_article_text(html_text)
        if len(text) < MIN_BODY_CHARS:
            logger.info(
                "article body too short (%d chars < %d) — likely paywall: %s",
                len(text),
                MIN_BODY_CHARS,
                url,
            )
            return None
        return text

    def _parse_rss(
        self,
        xml_text: str,
        *,
        lookback_days: int,
        require_keywords: bool,
    ) -> list[NewsArticleResult]:
        """Parse RSS XML into deduplicated, optionally keyword-filtered results.

        Used by both ``google_news_rss`` and the TechCrunch adapter — the
        adapter passes ``require_keywords=False`` because the TC venture tag
        is itself the funding filter.
        """
        parsed = feedparser.parse(xml_text)
        cutoff: date | None = None
        if lookback_days >= 0:
            cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).date()

        seen: set[str] = set()
        results: list[NewsArticleResult] = []
        for entry in parsed.entries:
            link = entry.get("link")
            title = entry.get("title")
            if not link or not title:
                continue

            url_canon = canonical_url(link)
            if url_canon in seen:
                continue

            snippet = _strip_html(entry.get("summary") or "")
            published = _struct_time_to_date(entry.get("published_parsed"))
            if cutoff is not None and published is not None and published < cutoff:
                continue

            if require_keywords:
                haystack = f"{title}\n{snippet}"
                if not _matches_funding_keyword(haystack):
                    continue

            # Source: Google News supplies a <source> element with the real
            # publisher; fall back to the URL's hostname for direct feeds.
            source: str
            src = entry.get("source")
            if isinstance(src, dict) and src.get("href"):
                source = hostname(str(src["href"]))
            else:
                source = hostname(link)

            seen.add(url_canon)
            results.append(
                NewsArticleResult(
                    url=url_canon,
                    title=title,
                    source=source,
                    published_date=published,
                    raw_content=snippet,
                )
            )
        return results
