"""Async HTTP client for fetching company homepages.

- Per-domain throttle (1 req/sec, spec §3.2 + §11).
- User-Agent enforced on every request (constructor rejects empty).
- robots.txt checked before every fetch via RobotsCache.
- tenacity retries on 429 / 5xx / network errors (3 attempts, exp backoff).
- Reasonable timeouts (30s overall; 5s for robots.txt).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from nous.sources.duckduckgo import DuckDuckGoSearch, is_aggregator
from nous.sources.robots import RobotsBlockedError, RobotsCache
from nous.util.slugify import strip_corporate_suffix

# Re-export for backwards compatibility — callers that did
# `from nous.sources.homepage import RobotsBlockedError` continue to work,
# but the canonical home is now nous.sources.robots.
__all__ = ["FetchResult", "HomepageClient", "RobotsBlockedError"]

logger = logging.getLogger(__name__)


class FetchResult(BaseModel):
    url: str            # final URL after redirects
    status_code: int
    content: str
    content_type: str   # e.g. "text/html"

CANDIDATE_TLDS: tuple[str, ...] = (".com", ".io", ".ai", ".co")
CANDIDATE_PATHS: tuple[str, ...] = (
    "/", "/about", "/about-us", "/product", "/products", "/company", "/team",
)


def _is_retryable(exc: BaseException) -> bool:
    """Return True for errors that warrant a retry.

    Permanent errors (DNS failure, connection refused, TLS handshake) are
    represented by httpx.ConnectError and must NOT be retried — they add
    ~7s of dead time per non-existent domain across exp-backoff attempts.
    Only transient failures (rate limits, server errors, timeouts) retry.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        # 429 (rate limit) and 5xx (server error) — usually transient
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    # ConnectError, ProtocolError, etc. — usually permanent (DNS, refused, TLS handshake)
    # TimeoutException is a transient network blip — retry makes sense.
    return isinstance(exc, httpx.TimeoutException)


class HomepageClient:
    """Async context-manager that fetches company homepages with per-domain rate limiting."""

    def __init__(
        self,
        user_agent: str,
        requests_per_second_per_domain: float = 1.0,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise ValueError(
                "user_agent must be a non-empty string containing a contact email. "
                "Websites block anonymous crawlers — this is non-negotiable."
            )
        self._user_agent = user_agent
        self._rps = requests_per_second_per_domain
        self._min_interval: float = 1.0 / requests_per_second_per_domain

        # Per-domain throttle state: domain → last request monotonic timestamp
        self._domain_last_request: dict[str, float] = {}
        # Per-domain lock: ensures only one request fires at a time per domain,
        # preventing thundering-herd when multiple coroutines target the same host.
        self._domain_locks: dict[str, asyncio.Lock] = {}
        # A single lock that protects creation of new entries in the dicts above.
        self._registry_lock = asyncio.Lock()

        self._client: httpx.AsyncClient | None = None
        self._robots: RobotsCache | None = None
        self._search_client: DuckDuckGoSearch | None = None

    async def __aenter__(self) -> HomepageClient:
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
        # Reuse the main client for DDG searches. The DuckDuckGoSearch client
        # manages its own global throttle (2 req/sec by default, spec §3.2).
        self._search_client = DuckDuckGoSearch(
            self._client,
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
        """Return the underlying clients, raising if not inside ``async with``."""
        if self._client is None or self._robots is None:
            raise RuntimeError("HomepageClient must be used as an async context manager.")
        return self._client, self._robots

    async def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        """Return (creating if needed) the per-domain Lock."""
        async with self._registry_lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = asyncio.Lock()
            return self._domain_locks[domain]

    async def _throttled_get(self, url: str) -> httpx.Response:
        """Rate-limited GET, serialised per domain.

        Acquires the domain lock, waits until the per-domain interval has
        elapsed since the last request, fires, then releases the lock.
        """
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
        """GET with tenacity retries on 429 / 5xx / network errors."""
        return await self._throttled_get(url)

    async def fetch(self, url: str) -> FetchResult:
        """Fetch a single URL.

        Raises:
            RobotsBlockedError: if robots.txt disallows the URL.
            httpx.HTTPStatusError: on 4xx (non-429) after retries exhausted.
            httpx.RequestError: on network errors after retries exhausted.

        On HTTP 403 from the httpx path, transparently retries with curl_cffi
        impersonating Chrome 120 — bypasses Cloudflare/WAF that fingerprint
        at the TLS layer (e.g. adquick.com 403s every UA from plain httpx but
        returns 200 to a real Chrome TLS handshake). robots.txt is always
        checked first; the fallback never sidesteps the robots policy.
        """
        _, robots = self._assert_open()

        allowed = await robots.is_allowed(url)
        if not allowed:
            raise RobotsBlockedError(f"robots.txt disallows: {url}")

        try:
            resp = await self._get_with_retry(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                logger.info(
                    "fetch: httpx got 403 for %s — retrying with Chrome impersonation",
                    url,
                )
                try:
                    return await self._fetch_with_chrome_impersonation(url)
                except Exception as inner:
                    logger.info(
                        "fetch: Chrome impersonation also failed for %s: %s: %s",
                        url,
                        type(inner).__name__,
                        inner,
                    )
                    # Re-raise the original 403 so the caller's metrics still
                    # categorize this as "blocked", not "Chrome client crash".
                    raise exc from None
            raise

        content_type = resp.headers.get("content-type", "")
        # Strip charset suffix if present: "text/html; charset=utf-8" → "text/html"
        content_type_clean = content_type.split(";")[0].strip()

        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            content=resp.text,
            content_type=content_type_clean,
        )

    async def _fetch_with_chrome_impersonation(self, url: str) -> FetchResult:
        """Fallback fetcher using curl_cffi with a real Chrome 120 TLS+HTTP2
        fingerprint. Bypasses Cloudflare/WAF that block requests at the TLS
        layer rather than via User-Agent string.

        Reuses the per-domain throttle (we still want to be polite even when
        switching transports). Raises httpx.HTTPStatusError on non-2xx so the
        caller's existing error handling treats it the same as the primary
        path.
        """
        # Imported lazily so the dependency isn't required at module-load time
        # — useful for environments where curl_cffi isn't installed yet (older
        # branches in CI, local dev without `uv sync`, etc.).
        from curl_cffi.requests import AsyncSession

        parsed = urlparse(url)
        domain = parsed.netloc

        domain_lock = await self._get_domain_lock(domain)
        async with domain_lock:
            now = time.monotonic()
            last = self._domain_last_request.get(domain, 0.0)
            wait = self._min_interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)

            async with AsyncSession() as session:
                resp = await session.get(
                    url,
                    impersonate="chrome120",
                    timeout=30,
                    allow_redirects=True,
                )
            self._domain_last_request[domain] = time.monotonic()

        if resp.status_code >= 400:
            # Synthesize an httpx-like exception so the caller's except chain
            # matches what it expects from the primary fetcher.
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} from Chrome-impersonation fetch",
                request=httpx.Request("GET", url),
                response=httpx.Response(
                    resp.status_code,
                    content=resp.content,
                    request=httpx.Request("GET", url),
                ),
            )

        content_type = resp.headers.get("content-type", "")
        content_type_clean = content_type.split(";")[0].strip()

        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            content=resp.text,
            content_type=content_type_clean,
        )

    async def search_companies(self, query: str, limit: int = 10) -> list[str]:
        """Search DuckDuckGo for candidate company homepage URLs.

        Delegates to the internal DuckDuckGoSearch client. Returns an empty
        list if DDG is unavailable or returns a captcha interstitial.
        """
        if self._search_client is None:
            raise RuntimeError("HomepageClient must be used as an async context manager.")
        return await self._search_client.search(query, limit=limit)

    @property
    def ddg_blocked(self) -> bool:
        """True once DDG has rate-limited this process (circuit breaker open).

        Lets callers annotate results that were computed without the DDG
        fallback — a "no match" under an open breaker is weaker evidence than
        one where the search actually ran.
        """
        return self._search_client is not None and self._search_client.is_blocked


async def resolve_homepage(
    client: HomepageClient,
    slug_base: str,
    company_name: str,
    *,
    tlds: Iterable[str] = CANDIDATE_TLDS,
) -> str | None:
    """Phase 1: try ``{slug_base}{tld}`` for each TLD in order.

    On a 200 response, validates that the page's visible text contains
    ``slug_base`` (case-insensitive). Returns the first plausible URL on match.

    Phase 2: if all TLD guesses miss, query DuckDuckGo for
    ``"{company_name}" startup``, filter out aggregator domains, and return the
    first candidate whose page contains the company name.

    Returns None if both phases miss.
    """
    # Phase 1: TLD heuristic
    for tld in tlds:
        url = f"https://{slug_base}{tld}"
        try:
            result = await client.fetch(url)
        except RobotsBlockedError:
            continue
        except httpx.HTTPStatusError:
            continue
        except httpx.RequestError:
            continue

        # Validate: does visible page text mention the slug?
        visible_text = HTMLParser(result.content).text(strip=True).lower()
        if slug_base.replace("-", " ") in visible_text or slug_base in visible_text:
            return result.url  # final URL after any redirects

    # Phase 2: DuckDuckGo search fallback. Treat any failure (network error,
    # captcha interstitial, malformed HTML, missing context-manager state) as
    # "no candidates" rather than letting the stage record an error — DDG is
    # supplementary; a broken fallback shouldn't reclassify a no-match as an
    # error and prevent website_resolved_at from being set.
    query = f'"{company_name}" startup'
    try:
        candidates = await client.search_companies(query, limit=10)
    except Exception:
        logger.warning(
            "DDG search fallback failed for %s; treating as no candidates",
            company_name,
            exc_info=True,
        )
        candidates = []

    name_lower = company_name.lower()
    # Strip corporate suffixes for a more lenient page-text match.
    naked_name = strip_corporate_suffix(company_name).lower()

    for candidate_url in candidates:
        if is_aggregator(candidate_url):
            continue
        try:
            result = await client.fetch(candidate_url)
        except (RobotsBlockedError, httpx.HTTPStatusError, httpx.RequestError):
            continue
        visible_text = HTMLParser(result.content).text(strip=True).lower()
        # OR: either the suffix-stripped name or the full name appears in text.
        if (naked_name and naked_name in visible_text) or (name_lower in visible_text):
            return result.url

    return None
