"""Tests for nous.sources._http — the shared per-domain throttle.

Covers the DomainThrottle contract (per-domain serialization, cross-domain
independence, stamping on failure, mixed intervals, reset) and the W-C.1
regression: two DIFFERENT transports (HomepageClient via httpx, and
HeadlessBrowserClient via mocked Playwright internals) hitting one host must
contend on the same lock and never fire closer than the min interval.

Intervals are short (50-200ms) so the whole module runs in well under 2s.
No real network, no real Chromium.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nous.sources._http import DEFAULT_THROTTLE, DomainThrottle, ThrottledHTTPClient
from nous.sources.headless_browser import HeadlessBrowserClient
from nous.sources.homepage import HomepageClient
from nous.sources.news import NewsClient

USER_AGENT = "nous-test test@example.com"


# ---------------------------------------------------------------------------
# DomainThrottle unit tests
# ---------------------------------------------------------------------------


async def test_same_domain_slots_serialize() -> None:
    """Two slots on one domain fire at least min_interval apart."""
    throttle = DomainThrottle()
    interval = 0.1
    times: list[float] = []

    for _ in range(2):
        async with throttle.slot("example.com", interval):
            times.append(time.monotonic())

    gap = times[1] - times[0]
    assert gap >= interval * 0.9, f"gap {gap:.3f}s < min interval {interval:.3f}s"


async def test_different_domains_do_not_wait_on_each_other() -> None:
    """Slots for distinct domains are independent — no shared lock, no wait."""
    throttle = DomainThrottle()
    interval = 0.2

    async def take(domain: str) -> None:
        async with throttle.slot(domain, interval):
            pass
        # Second slot on the same domain would wait; a different domain must not.

    start = time.monotonic()
    await asyncio.gather(take("alpha.com"), take("beta.com"))
    # First slot per domain never waits (no prior stamp), so this is ~instant.
    assert time.monotonic() - start < interval * 0.5


async def test_failed_request_still_stamps_timestamp() -> None:
    """An exception inside the slot still counts against the interval.

    A timeout or 5xx almost certainly hit the host, so the next request must
    wait the full interval, not fire immediately.
    """
    throttle = DomainThrottle()
    interval = 0.1

    with pytest.raises(RuntimeError):
        async with throttle.slot("example.com", interval):
            raise RuntimeError("simulated request failure")
    failed_at = time.monotonic()

    async with throttle.slot("example.com", interval):
        gap = time.monotonic() - failed_at
    assert gap >= interval * 0.9, f"gap after failure {gap:.3f}s < {interval:.3f}s"


async def test_mixed_intervals_share_one_lock() -> None:
    """Clients with different rates sharing a registry still serialize per domain,
    and each waits out its OWN interval relative to the shared timestamp."""
    throttle = DomainThrottle()
    fast, slow = 0.05, 0.2

    async with throttle.slot("example.com", fast):
        pass
    stamped = time.monotonic()

    # The slow client waits its full (longer) interval since the fast client's hit.
    async with throttle.slot("example.com", slow):
        slow_gap = time.monotonic() - stamped
    assert slow_gap >= slow * 0.9

    # And the fast client only waits its own (shorter) interval after the slow hit.
    stamped = time.monotonic()
    async with throttle.slot("example.com", fast):
        fast_gap = time.monotonic() - stamped
    assert fast_gap < slow  # did not inherit the slow client's interval


async def test_reset_clears_state() -> None:
    """After reset() a fresh slot fires immediately — test-isolation contract."""
    throttle = DomainThrottle()
    interval = 0.2

    async with throttle.slot("example.com", interval):
        pass
    throttle.reset()

    start = time.monotonic()
    async with throttle.slot("example.com", interval):
        pass
    assert time.monotonic() - start < interval * 0.5


# ---------------------------------------------------------------------------
# Cross-instance sharing: the W-C.1 fix
# ---------------------------------------------------------------------------


def test_clients_share_the_default_registry() -> None:
    """Independently constructed clients all land on DEFAULT_THROTTLE.

    This is the bug fix: per-instance registries let a second transport
    double-hit a host.
    """
    homepage = HomepageClient(user_agent=USER_AGENT)
    news = NewsClient(user_agent=USER_AGENT)
    browser = HeadlessBrowserClient()

    assert homepage._http.throttle is DEFAULT_THROTTLE
    assert news._http.throttle is DEFAULT_THROTTLE
    assert browser._throttle is DEFAULT_THROTTLE


def test_throttle_is_injectable_per_client() -> None:
    """A custom registry isolates a client from the process-wide default."""
    private = DomainThrottle()
    homepage = HomepageClient(user_agent=USER_AGENT, throttle=private)
    browser = HeadlessBrowserClient(throttle=private)

    assert homepage._http.throttle is private
    assert browser._throttle is private
    assert private is not DEFAULT_THROTTLE


async def test_two_throttled_http_clients_serialize_on_shared_registry() -> None:
    """Two ThrottledHTTPClient instances (one registry) queue on one domain."""
    registry = DomainThrottle()
    rps = 10.0  # 100ms interval
    a = ThrottledHTTPClient(requests_per_second_per_domain=rps, throttle=registry)
    b = ThrottledHTTPClient(requests_per_second_per_domain=rps, throttle=registry)

    hits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(time.monotonic())
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(
            a.get(client, "https://example.com/1"),
            b.get(client, "https://example.com/2"),
        )

    assert len(hits) == 2
    assert abs(hits[1] - hits[0]) >= (1.0 / rps) * 0.9


# ---------------------------------------------------------------------------
# Regression: httpx transport + Playwright transport serialize on one host
# ---------------------------------------------------------------------------


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Mock transport that timestamps every non-robots hit to the target host."""

    def __init__(self, hits: list[float]) -> None:
        self._hits = hits

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "robots.txt" in str(request.url):
            return httpx.Response(404, content=b"Not Found")
        self._hits.append(time.monotonic())
        return httpx.Response(
            200,
            content=b"<html><body>ok</body></html>",
            headers={"content-type": "text/html"},
        )


def _stub_playwright_browser(hits: list[float]) -> AsyncMock:
    """A fake Playwright Browser whose page.goto timestamps the 'request'."""
    page = AsyncMock()

    async def _goto(url: str, **kwargs: object) -> None:
        hits.append(time.monotonic())

    page.goto = AsyncMock(side_effect=_goto)
    page.content = AsyncMock(return_value="<html><body>rendered</body></html>")

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser


async def test_httpx_and_browser_transports_serialize_per_domain() -> None:
    """W-C.1 regression: two transports on one host never fire closer than
    the min interval, because default-constructed clients share one registry.

    Before the fix each client kept its own lock dict, so the browser fallback
    fired immediately after (or concurrently with) an httpx fetch to the same
    host — violating the 1 req/s/domain politeness rule.
    """
    rps = 5.0  # 200ms interval; 3 requests ≈ 400ms of throttle wait total
    min_interval = 1.0 / rps
    hits: list[float] = []

    homepage = HomepageClient(user_agent=USER_AGENT, requests_per_second_per_domain=rps)
    browser = HeadlessBrowserClient(
        requests_per_second_per_domain=rps,
        post_load_wait_ms=0,
    )
    # Stub the Chromium internals — no real browser; goto records the hit.
    browser._browser = _stub_playwright_browser(hits)

    async with homepage:
        # Swap in the recording transport (same pattern as test_homepage.py).
        assert homepage._client is not None
        assert homepage._robots is not None
        transport = _RecordingTransport(hits)
        homepage._client = httpx.AsyncClient(
            transport=transport, headers={"User-Agent": USER_AGENT}
        )
        homepage._robots._client = httpx.AsyncClient(
            transport=transport, headers={"User-Agent": USER_AGENT}
        )

        # assert_public_url does real DNS — neutralize it for the fake host
        # (same precedent as test_homepage_chrome_fallback.py).
        with patch(
            "nous.sources.headless_browser.assert_public_url",
            new=AsyncMock(return_value=None),
        ):
            await asyncio.gather(
                homepage.fetch("https://example.com/a"),
                browser.fetch_rendered_html("https://example.com/b"),
                homepage.fetch("https://example.com/c"),
            )

    assert len(hits) == 3
    hits.sort()
    gaps = [later - earlier for earlier, later in zip(hits, hits[1:], strict=False)]
    assert all(gap >= min_interval * 0.9 for gap in gaps), (
        f"transports double-hit the host: gaps {[f'{g:.3f}' for g in gaps]} "
        f"vs required {min_interval:.3f}s"
    )
