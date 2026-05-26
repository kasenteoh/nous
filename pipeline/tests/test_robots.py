"""Tests for nous.sources.robots — RobotsCache."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import nous.sources.robots as robots_module
from nous.sources.robots import RobotsCache

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transport(
    routes: dict[str, tuple[int, str]],
) -> httpx.AsyncBaseTransport:
    """Create a mock transport from {url_suffix: (status, body)} mapping."""

    class _Transport(httpx.AsyncBaseTransport):
        def __init__(self, routes: dict[str, tuple[int, str]]) -> None:
            self._routes = routes
            self.request_count = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.request_count += 1
            url_str = str(request.url)
            for suffix, (status, body) in self._routes.items():
                if suffix in url_str:
                    return httpx.Response(
                        status,
                        content=body.encode(),
                        headers={"content-type": "text/plain"},
                    )
            # Default: 404
            return httpx.Response(404, content=b"Not Found")

    return _Transport(routes)


def _make_cache(transport: httpx.AsyncBaseTransport) -> RobotsCache:
    client = httpx.AsyncClient(transport=transport)
    return RobotsCache(client=client, user_agent="nous-test test@example.com")


# ---------------------------------------------------------------------------
# is_allowed: missing robots.txt (404) → allow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_allowed_when_robots_missing() -> None:
    """A 404 on robots.txt means the site has no restrictions → allow."""
    transport = _make_transport({})
    cache = _make_cache(transport)

    assert await cache.is_allowed("https://example.com/some/page") is True


# ---------------------------------------------------------------------------
# is_allowed: allowed path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_allowed_for_permitted_path() -> None:
    robots_txt = (FIXTURES / "sample_robots_disallow.txt").read_text()
    transport = _make_transport({"robots.txt": (200, robots_txt)})
    cache = _make_cache(transport)

    assert await cache.is_allowed("https://example.com/public/page") is True


# ---------------------------------------------------------------------------
# is_allowed: disallowed path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_disallowed_for_secret_path() -> None:
    robots_txt = (FIXTURES / "sample_robots_disallow.txt").read_text()
    transport = _make_transport({"robots.txt": (200, robots_txt)})
    cache = _make_cache(transport)

    assert await cache.is_allowed("https://example.com/secret/data") is False


# ---------------------------------------------------------------------------
# Cache reuse within TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_reuses_parser_within_ttl() -> None:
    """Two is_allowed calls for the same domain should only fetch robots.txt once."""
    robots_txt = (FIXTURES / "sample_robots_disallow.txt").read_text()

    class CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.fetch_count = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.fetch_count += 1
            return httpx.Response(
                200,
                content=robots_txt.encode(),
                headers={"content-type": "text/plain"},
            )

    transport = CountingTransport()
    cache = _make_cache(transport)

    await cache.is_allowed("https://example.com/page1")
    await cache.is_allowed("https://example.com/page2")

    assert transport.fetch_count == 1  # Only fetched once


# ---------------------------------------------------------------------------
# Cache refetches after TTL expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_refetches_after_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """After TTL expiry, a second call to is_allowed should re-fetch robots.txt."""
    robots_txt = (FIXTURES / "sample_robots_disallow.txt").read_text()

    class CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.fetch_count = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.fetch_count += 1
            return httpx.Response(
                200,
                content=robots_txt.encode(),
                headers={"content-type": "text/plain"},
            )

    transport = CountingTransport()
    cache = _make_cache(transport)

    # First fetch
    await cache.is_allowed("https://example.com/page1")
    assert transport.fetch_count == 1

    # Simulate TTL expiry by patching the module-level constant to 0
    monkeypatch.setattr(robots_module, "ROBOTS_CACHE_TTL_SECONDS", 0)

    # Second fetch — cache entry is now considered expired
    await cache.is_allowed("https://example.com/page2")
    assert transport.fetch_count == 2
