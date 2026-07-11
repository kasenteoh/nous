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
- Per-domain throttle is honored via the process-wide registry in
  :mod:`nous.sources._http` — the SAME registry
  :class:`nous.sources.homepage.HomepageClient` uses by default, so the two
  transports genuinely take turns on a host instead of double-hitting it.
- robots.txt is the caller's responsibility (the scrape stage checks it once
  via HomepageClient before either path runs).
- A new browser context per page (cheap) gives us a clean cookie jar /
  origin per fetch, avoiding cross-site cookie leakage.

Tested in M3 against anspect-technologies.com (0 chars via httpx →
~2130 chars via Playwright) and phia.com (0 chars via httpx → ~2787 chars
via Playwright).
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import TYPE_CHECKING

from nous.sources._http import DEFAULT_THROTTLE, DomainThrottle
from nous.util.ssrf import BlockedAddressError, assert_public_url

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright, Route


# Default UA presents as a real Chrome on macOS — paired with Chromium's
# actual TLS/HTTP2 fingerprint, this gets past Cloudflare basic mode (same
# logic as curl_cffi's chrome120 impersonation, but with a real browser).
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def _abort_if_blocked(route: Route) -> None:
    """Abort any request (initial, redirect, or sub-resource) to a non-public host."""
    try:
        await assert_public_url(route.request.url)
    except BlockedAddressError:
        await route.abort()
    else:
        await route.continue_()


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
        throttle: DomainThrottle | None = None,
    ) -> None:
        self._user_agent = user_agent or _DEFAULT_USER_AGENT
        self._min_interval: float = 1.0 / requests_per_second_per_domain
        self._navigation_timeout_ms = navigation_timeout_ms
        self._post_load_wait_ms = post_load_wait_ms

        # Process-wide throttle registry by default — shared with
        # HomepageClient (and every other transport) so a browser-fallback
        # fetch queues behind an httpx fetch to the same host.
        self._throttle = throttle if throttle is not None else DEFAULT_THROTTLE

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

    async def fetch_rendered_html(self, url: str) -> str | None:
        """Navigate to ``url``, wait for JS hydration, return rendered HTML.

        Returns None on navigation timeout / browser error so the caller can
        fall back to whatever the httpx-side response was. Never raises for
        Playwright errors (logged and swallowed) — but DOES raise
        BlockedAddressError for an internal/non-public target, validated before
        any browser interaction.
        """
        # SSRF guard FIRST — before touching the browser — so a blocked URL is
        # rejected even outside the context manager. The context.route handler
        # below additionally re-validates every redirect hop and sub-resource.
        await assert_public_url(url)

        if self._browser is None:
            raise RuntimeError(
                "HeadlessBrowserClient must be used as an async context manager"
            )

        # Shared slot with the httpx transports; the slot stamps the domain's
        # timestamp on exit whether the navigation succeeded or failed (a
        # failed goto still hit the host).
        async with self._throttle.slot_for_url(url, self._min_interval):
            context = await self._browser.new_context(user_agent=self._user_agent)
            await context.route("**/*", _abort_if_blocked)
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
                return await page.content()
            except Exception as exc:
                logger.info(
                    "headless-browser: fetch failed for %s: %s: %s",
                    url,
                    type(exc).__name__,
                    exc,
                )
                return None
            finally:
                await page.close()
                await context.close()
