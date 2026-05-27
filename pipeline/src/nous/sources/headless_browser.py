"""Headless Chromium fallback for JS-rendered SPAs that httpx can't extract.

When ``HomepageClient.fetch()`` succeeds but the static HTML has effectively
no visible text (e.g. a Next.js / React shell with an empty ``<div id="__next">``
waiting for hydration), the scrape stage falls back here to launch a real
Chromium browser, navigate to the URL, wait for hydration, and capture the
rendered DOM. The resulting HTML — which now contains the JS-rendered body
content — is what gets stored in ``raw_pages.content`` and ultimately fed to
the enrichment LLM.

Design:
- One Chromium process per scrape-stage run, shared across all per-company
  fetches. Launch cost (~3-5s) amortizes; per-page cost is mostly the page's
  own JS hydration time.
- Per-domain throttle is honored via the shared lock dict on this client —
  same semantics as :class:`nous.sources.homepage.HomepageClient` so we don't
  blast a single domain from two transports.
- robots.txt is the caller's responsibility (the scrape stage checks it once
  via HomepageClient before either path runs).
- A new browser context per page (cheap) gives us a clean cookie jar /
  origin per fetch, avoiding cross-site cookie leakage.

Tested in M3 against anspect-technologies.com (0 chars via httpx →
~2130 chars via Playwright) and phia.com (0 chars via httpx → ~2787 chars
via Playwright).
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import TracebackType
from typing import TYPE_CHECKING
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright


# Default UA presents as a real Chrome on macOS — paired with Chromium's
# actual TLS/HTTP2 fingerprint, this gets past Cloudflare basic mode (same
# logic as curl_cffi's chrome120 impersonation, but with a real browser).
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class HeadlessBrowserClient:
    """Async context manager wrapping a headless Chromium browser.

    Usage::

        async with HeadlessBrowserClient() as browser:
            html = await browser.fetch_rendered_html("https://example.com/")
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        requests_per_second_per_domain: float = 1.0,
        navigation_timeout_ms: int = 30_000,
        post_load_wait_ms: int = 1_500,
    ) -> None:
        self._user_agent = user_agent or _DEFAULT_USER_AGENT
        self._min_interval: float = 1.0 / requests_per_second_per_domain
        self._navigation_timeout_ms = navigation_timeout_ms
        self._post_load_wait_ms = post_load_wait_ms

        # Per-domain throttle state — mirrors HomepageClient so the two
        # transports cooperate when targeting the same host.
        self._domain_last_request: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> HeadlessBrowserClient:
        # Lazy import — keeps the rest of the codebase importable even on
        # environments where Playwright wheels aren't installed yet (CI
        # caches, fresh worktrees, etc.).
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        async with self._registry_lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = asyncio.Lock()
            return self._domain_locks[domain]

    async def fetch_rendered_html(self, url: str) -> str | None:
        """Navigate to ``url``, wait for JS hydration, return rendered HTML.

        Returns None on navigation timeout / browser error so the caller can
        fall back to whatever the httpx-side response was. Never raises;
        Playwright errors are logged and swallowed.
        """
        if self._browser is None:
            raise RuntimeError(
                "HeadlessBrowserClient must be used as an async context manager"
            )

        parsed = urlparse(url)
        domain = parsed.netloc

        domain_lock = await self._get_domain_lock(domain)
        async with domain_lock:
            now = time.monotonic()
            last = self._domain_last_request.get(domain, 0.0)
            wait = self._min_interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)

            context = await self._browser.new_context(user_agent=self._user_agent)
            page = await context.new_page()
            try:
                await page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=self._navigation_timeout_ms,
                )
                # Brief extra wait after networkidle — React/Next.js
                # frameworks sometimes commit a final hydration tick after
                # the network calms down.
                await page.wait_for_timeout(self._post_load_wait_ms)
                content = await page.content()
                self._domain_last_request[domain] = time.monotonic()
                return content
            except Exception as exc:
                logger.info(
                    "headless-browser: fetch failed for %s: %s: %s",
                    url,
                    type(exc).__name__,
                    exc,
                )
                self._domain_last_request[domain] = time.monotonic()
                return None
            finally:
                await page.close()
                await context.close()
