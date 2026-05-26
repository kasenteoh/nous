"""Async HTTP client for fetching company homepages.

- Per-domain throttle (1 req/sec, spec §3.2 + §11).
- User-Agent enforced on every request (constructor rejects empty).
- robots.txt checked before every fetch via RobotsCache.
- tenacity retries on 429 / 5xx / network errors (3 attempts, exp backoff).
- Reasonable timeouts (30s overall; 5s for robots.txt).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from nous.sources.robots import RobotsCache


class FetchResult(BaseModel):
    url: str            # final URL after redirects
    status_code: int
    content: str
    content_type: str   # e.g. "text/html"


class RobotsBlockedError(Exception):
    """Raised when robots.txt forbids the URL — caller decides what to do."""


CANDIDATE_TLDS: tuple[str, ...] = (".com", ".io", ".ai", ".co")
CANDIDATE_PATHS: tuple[str, ...] = (
    "/", "/about", "/about-us", "/product", "/products", "/company", "/team",
)


def _is_retryable(exc: BaseException) -> bool:
    """Return True for errors that warrant a retry."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.RequestError)


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
        """
        _, robots = self._assert_open()

        allowed = await robots.is_allowed(url)
        if not allowed:
            raise RobotsBlockedError(f"robots.txt disallows: {url}")

        resp = await self._get_with_retry(url)

        content_type = resp.headers.get("content-type", "")
        # Strip charset suffix if present: "text/html; charset=utf-8" → "text/html"
        content_type_clean = content_type.split(";")[0].strip()

        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            content=resp.text,
            content_type=content_type_clean,
        )


async def resolve_homepage(
    client: HomepageClient,
    slug_base: str,
    company_name: str,
    *,
    tlds: Iterable[str] = CANDIDATE_TLDS,
) -> str | None:
    """Try ``{slug_base}{tld}`` for each TLD in order.

    On a 200 response, validates that the page's visible text contains
    ``slug_base`` (case-insensitive). Returns the first plausible URL or None.

    slug_base must already be the normalized name (lowercase, no corporate
    suffixes). The caller (Chunk 4) is responsible for normalization.
    company_name is accepted for interface compatibility but slug_base drives
    the match — slug_base is already derived from company_name.

    No search-engine fallback is attempted (DDG deferred to M5 per spec).
    """
    # Suppress unused parameter warning — retained in signature for caller
    _ = company_name

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
        if slug_base in visible_text:
            return result.url  # final URL after any redirects

    return None
