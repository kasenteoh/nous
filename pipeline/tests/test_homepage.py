"""Tests for nous.sources.homepage — HomepageClient and resolve_homepage.

Uses httpx mock transports (same pattern as test_edgar.py). No real network
calls are made.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from nous.sources.homepage import (
    FetchResult,
    HomepageClient,
    RobotsBlockedError,
    _is_retryable,
    resolve_homepage,
)
from nous.util.ssrf import BlockedAddressError

FIXTURES = Path(__file__).parent / "fixtures"

ROBOTS_DISALLOW = (FIXTURES / "sample_robots_disallow.txt").read_text()
HTML_WITH_NAME = (FIXTURES / "sample_homepage_with_name.html").read_text()
HTML_NO_NAME = (FIXTURES / "sample_homepage_no_name.html").read_text()

USER_AGENT = "nous-test test@example.com"


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------


class RouteSpec:
    """One route entry for MockTransport."""

    def __init__(
        self,
        url_contains: str,
        *,
        status: int = 200,
        body: str = "",
        content_type: str = "text/html",
        raise_network_error: bool = False,
    ) -> None:
        self.url_contains = url_contains
        self.status = status
        self.body = body
        self.content_type = content_type
        self.raise_network_error = raise_network_error
        self.call_count = 0

    def matches(self, request: httpx.Request) -> bool:
        return self.url_contains in str(request.url)


class MockTransport(httpx.AsyncBaseTransport):
    """Dispatches to the first matching RouteSpec; 404 if none match."""

    def __init__(self, routes: list[RouteSpec]) -> None:
        self._routes = routes
        self.total_calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.total_calls += 1
        for route in self._routes:
            if route.matches(request):
                route.call_count += 1
                if route.raise_network_error:
                    raise httpx.ConnectError("Connection refused")
                resp = httpx.Response(
                    route.status,
                    content=route.body.encode(),
                    headers={"content-type": route.content_type},
                )
                if route.status >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {route.status}",
                        request=request,
                        response=resp,
                    )
                return resp
        # No matching route → 404 (common for robots.txt probes on unknown hosts)
        return httpx.Response(404, content=b"Not Found")


def _inject_transport(client: HomepageClient, transport: MockTransport) -> None:
    """Replace the real httpx clients with a mock transport after __aenter__."""
    assert client._client is not None
    assert client._robots is not None
    client._client = httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    client._robots._client = httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# _is_retryable
# ---------------------------------------------------------------------------


def test_is_retryable_connect_error_is_false() -> None:
    """DNS failure / connection refused must NOT be retried — permanent error."""
    exc = httpx.ConnectError("no such host")
    assert _is_retryable(exc) is False


def test_is_retryable_connect_timeout_is_true() -> None:
    """ConnectTimeout is a TimeoutException subclass — transient, should retry."""
    exc = httpx.ConnectTimeout("timed out")
    assert _is_retryable(exc) is True


def test_is_retryable_read_timeout_is_true() -> None:
    """ReadTimeout is a TimeoutException subclass — transient, should retry."""
    exc = httpx.ReadTimeout("read timed out")
    assert _is_retryable(exc) is True


def test_is_retryable_429_is_true() -> None:
    """429 rate-limit responses should be retried."""
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("429", request=request, response=response)
    assert _is_retryable(exc) is True


def test_is_retryable_500_is_true() -> None:
    """5xx server errors should be retried."""
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(500, request=request)
    exc = httpx.HTTPStatusError("500", request=request, response=response)
    assert _is_retryable(exc) is True


def test_is_retryable_404_is_false() -> None:
    """4xx (non-429) client errors are permanent — should not retry."""
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("404", request=request, response=response)
    assert _is_retryable(exc) is False


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_empty_user_agent_raises() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        HomepageClient(user_agent="")


def test_whitespace_user_agent_raises() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        HomepageClient(user_agent="   ")


# ---------------------------------------------------------------------------
# Successful fetch
# ---------------------------------------------------------------------------


async def test_successful_fetch_returns_fetch_result() -> None:
    routes = [
        RouteSpec("example.com/robots.txt", status=404),
        RouteSpec("example.com", status=200, body="<html><body>Hello</body></html>"),
    ]
    transport = MockTransport(routes)

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await client.fetch("https://example.com/")

    assert isinstance(result, FetchResult)
    assert result.status_code == 200
    assert "Hello" in result.content
    assert result.content_type == "text/html"
    assert "example.com" in result.url


# ---------------------------------------------------------------------------
# Robots-blocked URL raises RobotsBlockedError without making GET request
# ---------------------------------------------------------------------------


async def test_robots_blocked_raises_and_no_get() -> None:
    """When robots.txt disallows a URL, we must raise before making the GET."""
    page_route = RouteSpec("example.com/secret/", status=200, body="secret page")
    robots_route = RouteSpec("example.com/robots.txt", status=200, body=ROBOTS_DISALLOW)
    transport = MockTransport([robots_route, page_route])

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        with pytest.raises(RobotsBlockedError):
            await client.fetch("https://example.com/secret/data")

    # The page route must never have been hit
    assert page_route.call_count == 0


# ---------------------------------------------------------------------------
# Per-domain throttle: same domain serialised
# ---------------------------------------------------------------------------


async def test_per_domain_throttle_same_domain() -> None:
    """Two consecutive fetches to the same domain wait at least 1/rps seconds apart."""
    rps = 10.0  # fast enough that the test only takes ~100ms
    min_interval = 1.0 / rps

    routes = [
        RouteSpec("example.com/robots.txt", status=404),
        RouteSpec("example.com", status=200, body="<html><body>ok</body></html>"),
    ]
    transport = MockTransport(routes)

    timestamps: list[float] = []

    client = HomepageClient(user_agent=USER_AGENT, requests_per_second_per_domain=rps)
    async with client:
        _inject_transport(client, transport)
        t0 = time.monotonic()
        await client.fetch("https://example.com/page1")
        timestamps.append(time.monotonic() - t0)
        await client.fetch("https://example.com/page2")
        timestamps.append(time.monotonic() - t0)

    gap = timestamps[1] - timestamps[0]
    assert gap >= min_interval * 0.9, f"Gap {gap:.3f}s < expected {min_interval:.3f}s"


# ---------------------------------------------------------------------------
# Per-domain throttle: different domains run in parallel
# ---------------------------------------------------------------------------


async def test_different_domains_run_in_parallel() -> None:
    """Two fetches to different domains should complete faster than sequential."""
    rps = 5.0  # 200ms per domain
    min_interval = 1.0 / rps

    routes = [
        RouteSpec("alpha.com/robots.txt", status=404),
        RouteSpec("beta.com/robots.txt", status=404),
        RouteSpec("alpha.com", status=200, body="<html><body>alpha</body></html>"),
        RouteSpec("beta.com", status=200, body="<html><body>beta</body></html>"),
    ]
    transport = MockTransport(routes)

    client = HomepageClient(user_agent=USER_AGENT, requests_per_second_per_domain=rps)
    async with client:
        _inject_transport(client, transport)

        # Warm up each domain with one fetch so the next pair starts throttled
        await client.fetch("https://alpha.com/warmup")
        await client.fetch("https://beta.com/warmup")

        # Now fire both second fetches concurrently
        start = time.monotonic()
        await asyncio.gather(
            client.fetch("https://alpha.com/second"),
            client.fetch("https://beta.com/second"),
        )
        elapsed = time.monotonic() - start

    # If they ran sequentially, elapsed ≥ 2 * min_interval.
    # In parallel, elapsed should be ~min_interval (one delay, not two stacked).
    # We give generous headroom: just check elapsed < 1.8 * min_interval.
    assert elapsed < 1.8 * min_interval, (
        f"Elapsed {elapsed:.3f}s suggests domains weren't fetched in parallel "
        f"(expected < {1.8 * min_interval:.3f}s)"
    )


# ---------------------------------------------------------------------------
# 429 → 200 retry
# ---------------------------------------------------------------------------


class RetryTransport(httpx.AsyncBaseTransport):
    """Returns 429 on first call to target, 200 on subsequent calls."""

    def __init__(self, target_contains: str, success_body: str) -> None:
        self._target = target_contains
        self._success_body = success_body
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)

        # Always return 404 for robots.txt probes
        if "robots.txt" in url_str:
            return httpx.Response(404, content=b"Not Found")

        if self._target in url_str:
            self.call_count += 1
            if self.call_count == 1:
                resp = httpx.Response(429, content=b"Too Many Requests")
                raise httpx.HTTPStatusError("429", request=request, response=resp)
            return httpx.Response(
                200,
                content=self._success_body.encode(),
                headers={"content-type": "text/html"},
            )

        return httpx.Response(404, content=b"Not Found")


async def test_retry_on_429_succeeds() -> None:
    transport = RetryTransport("example.com", "<html><body>ok</body></html>")
    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await client.fetch("https://example.com/page")

    assert result.status_code == 200
    assert transport.call_count == 2  # first 429, second 200


# ---------------------------------------------------------------------------
# resolve_homepage
# ---------------------------------------------------------------------------


class ResolverTransport(httpx.AsyncBaseTransport):
    """
    Custom transport for resolve_homepage tests.

    Routes:
    - Any robots.txt → 404 (allow everything)
    - Configured exact-host → configured response
    - Everything else → 404
    """

    def __init__(self, host_responses: dict[str, tuple[int, str]]) -> None:
        """host_responses maps hostname (no scheme) → (status, body)."""
        self._host_responses = host_responses

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        host = request.url.host

        if "robots.txt" in url_str:
            return httpx.Response(404, content=b"Not Found")

        if host in self._host_responses:
            status, body = self._host_responses[host]
            resp = httpx.Response(
                status,
                content=body.encode(),
                headers={"content-type": "text/html"},
            )
            if status >= 400:
                raise httpx.HTTPStatusError(
                    f"HTTP {status}", request=request, response=resp
                )
            return resp

        return httpx.Response(404, content=b"Not Found")


async def test_resolve_homepage_finds_matching_name() -> None:
    """slug 'acme' → acme.com returns name-matching HTML → return that URL."""
    transport = ResolverTransport({"acme.com": (200, HTML_WITH_NAME)})

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(client, "acme", "Acme Corp")

    assert result is not None
    assert "acme.com" in result


async def test_resolve_homepage_returns_none_when_all_404() -> None:
    """slug 'ghostco' → all TLDs return 404 → None."""
    transport = ResolverTransport({})  # everything 404

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(client, "ghostco", "Ghost Co")

    assert result is None


class BlockingTransport(httpx.AsyncBaseTransport):
    """Transport that raises BlockedAddressError for every request.

    Mirrors the production SsrfGuardedAsyncTransport's behaviour against a host
    that resolves to (or is) an internal/unresolvable address: every hop —
    including the robots.txt probe — is rejected before a socket opens.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise BlockedAddressError(f"blocked: {request.url}")


async def test_resolve_homepage_skips_blocked_candidates() -> None:
    """Every candidate raises BlockedAddressError → all skipped, return None.

    Regression guard: a blocked/unresolvable candidate must be treated like a
    connection error (skip and try the next), not bubble up and error the whole
    company.
    """
    transport = BlockingTransport()

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(client, "acme", "Acme Corp")

    assert result is None


async def test_resolve_homepage_rejects_parked_domain() -> None:
    """slug 'parked' → page without the name in text → None."""
    transport = ResolverTransport({"parked.com": (200, HTML_NO_NAME)})

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(client, "parked", "Parked Co")

    assert result is None


async def test_resolve_homepage_tries_tlds_in_order() -> None:
    """Given .com → 404, .io → match, .ai → match: should return .io first."""
    transport = ResolverTransport(
        {
            "acme.io": (200, HTML_WITH_NAME),  # acme appears in fixture
            "acme.ai": (200, HTML_WITH_NAME),
        }
    )

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "acme",
            "Acme Corp",
            tlds=(".com", ".io", ".ai"),
        )

    assert result is not None
    assert "acme.io" in result  # .io comes before .ai


# ---------------------------------------------------------------------------
# resolve_homepage — Phase 2 DDG fallback
# ---------------------------------------------------------------------------


class MockSearchHomepageClient(HomepageClient):
    """HomepageClient subclass that overrides search_companies with a canned list."""

    def __init__(self, search_results: list[str], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._mock_search_results = search_results

    async def search_companies(self, query: str, limit: int = 10) -> list[str]:
        return self._mock_search_results[:limit]


async def test_resolve_homepage_phase2_ddg_fallback() -> None:
    """When TLD phase misses, DDG search returns a valid candidate → return it."""
    # TLD phase: all 404
    # DDG: first result is aggregator (linkedin.com), second is real homepage
    real_homepage_host = "4ldata.com"
    real_homepage_html = (
        "<html><body><h1>4L Data Intelligence</h1><p>AI startup.</p></body></html>"
    )

    search_results = [
        "https://www.linkedin.com/company/4l-data",  # aggregator — must be skipped
        f"https://{real_homepage_host}/",             # real homepage
    ]

    transport = ResolverTransport(
        {
            real_homepage_host: (200, real_homepage_html),
        }
    )

    client = MockSearchHomepageClient(
        search_results=search_results,
        user_agent=USER_AGENT,
    )
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "4l-data-intelligence",
            "4L Data Intelligence",
            tlds=(".com", ".io"),  # won't match 4ldata.com
        )

    assert result is not None
    assert real_homepage_host in result


async def test_resolve_homepage_phase2_skips_aggregators() -> None:
    """When DDG only returns aggregator URLs, phase 2 returns None."""
    search_results = [
        "https://www.linkedin.com/company/ghostco",
        "https://crunchbase.com/organization/ghostco",
    ]

    transport = ResolverTransport({})  # nothing reachable

    client = MockSearchHomepageClient(
        search_results=search_results,
        user_agent=USER_AGENT,
    )
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "ghostco",
            "GhostCo",
        )

    assert result is None


async def test_resolve_homepage_phase2_rejects_no_name_match() -> None:
    """DDG candidate fetched but doesn't mention company name → skip, return None."""
    # Page exists but doesn't mention the company name
    search_results = ["https://unrelated.com/"]
    transport = ResolverTransport(
        {"unrelated.com": (200, HTML_NO_NAME)}  # no mention of 'ghostco'
    )

    client = MockSearchHomepageClient(
        search_results=search_results,
        user_agent=USER_AGENT,
    )
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "ghostco",
            "GhostCo",
        )

    assert result is None
