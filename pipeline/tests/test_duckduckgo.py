"""Tests for nous.sources.duckduckgo — DuckDuckGoSearch client and helpers.

Uses httpx mock transports. No real network calls are made.
"""

from __future__ import annotations

import time

import httpx
import pytest

from nous.sources.duckduckgo import (
    DuckDuckGoSearch,
    _extract_result_urls,
    is_aggregator,
)

USER_AGENT = "nous-test test@example.com"

# ---------------------------------------------------------------------------
# Canned DDG HTML responses
# ---------------------------------------------------------------------------

# Minimal DDG HTML page with two result__a anchors:
#   1. A DDG redirect URL (with uddg param)
#   2. A direct URL
DDG_RESULTS_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="results">
  <div class="result">
    <a class="result__a" href="/l/?kh=-1&uddg=https%3A%2F%2Fexample.com%2F">Example Corp</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://another.io/about">Another Co</a>
  </div>
  <div class="result">
    <a class="result__a" href="/l/?kh=-1&uddg=https%3A%2F%2Fthird.ai%2F">Third AI</a>
  </div>
</div>
</body>
</html>
"""

# DDG captcha / anti-bot interstitial — contains "anomaly"
DDG_CAPTCHA_HTML = """
<!DOCTYPE html>
<html>
<body>
<p>Your request has been flagged as an anomaly. Please complete the CAPTCHA.</p>
</body>
</html>
"""

# Empty results page (no result__a anchors)
DDG_EMPTY_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="no-results">No results found.</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Mock transport
# ---------------------------------------------------------------------------


class DDGMockTransport(httpx.AsyncBaseTransport):
    """Returns a canned response for any POST to DDG_HTML_URL."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: str = DDG_RESULTS_HTML,
        raise_error: bool = False,
    ) -> None:
        self._status = status
        self._body = body
        self._raise_error = raise_error
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        if self._raise_error:
            raise httpx.ConnectError("connection refused")
        resp = httpx.Response(
            self._status,
            content=self._body.encode(),
            headers={"content-type": "text/html"},
        )
        if self._status >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self._status}", request=request, response=resp
            )
        return resp


def _make_client(transport: DDGMockTransport) -> tuple[httpx.AsyncClient, DuckDuckGoSearch]:
    http_client = httpx.AsyncClient(transport=transport, follow_redirects=True)
    search = DuckDuckGoSearch(
        http_client,
        user_agent=USER_AGENT,
        seconds_between_requests=0.0,  # no throttle in unit tests
    )
    return http_client, search


# ---------------------------------------------------------------------------
# _extract_result_urls
# ---------------------------------------------------------------------------


def test_extract_result_urls_parses_redirect_and_direct() -> None:
    """Both /l/?uddg=... redirects and direct hrefs are extracted in order."""
    urls = list(_extract_result_urls(DDG_RESULTS_HTML, limit=10))
    assert urls == [
        "https://example.com/",
        "https://another.io/about",
        "https://third.ai/",
    ]


def test_extract_result_urls_respects_limit() -> None:
    urls = list(_extract_result_urls(DDG_RESULTS_HTML, limit=2))
    assert len(urls) == 2
    assert urls[0] == "https://example.com/"
    assert urls[1] == "https://another.io/about"


def test_extract_result_urls_empty_page() -> None:
    urls = list(_extract_result_urls(DDG_EMPTY_HTML, limit=10))
    assert urls == []


def test_extract_result_urls_deduplicates() -> None:
    html = """
    <a class="result__a" href="https://example.com/">A</a>
    <a class="result__a" href="https://example.com/">A duplicate</a>
    <a class="result__a" href="https://other.com/">B</a>
    """
    urls = list(_extract_result_urls(html, limit=10))
    assert urls == ["https://example.com/", "https://other.com/"]


# ---------------------------------------------------------------------------
# Captcha / block detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_empty_on_captcha() -> None:
    transport = DDGMockTransport(status=200, body=DDG_CAPTCHA_HTML)
    http_client, search = _make_client(transport)
    try:
        results = await search.search("test query")
    finally:
        await http_client.aclose()
    assert results == []


@pytest.mark.asyncio
async def test_search_returns_empty_on_network_error() -> None:
    transport = DDGMockTransport(raise_error=True)
    http_client, search = _make_client(transport)
    try:
        results = await search.search("test query")
    finally:
        await http_client.aclose()
    assert results == []


@pytest.mark.asyncio
async def test_search_returns_empty_on_non_200() -> None:
    transport = DDGMockTransport(status=503)
    http_client, search = _make_client(transport)
    try:
        results = await search.search("test query")
    finally:
        await http_client.aclose()
    assert results == []


# ---------------------------------------------------------------------------
# Successful search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_urls_in_order() -> None:
    transport = DDGMockTransport(body=DDG_RESULTS_HTML)
    http_client, search = _make_client(transport)
    try:
        results = await search.search("example company startup", limit=10)
    finally:
        await http_client.aclose()
    assert results == [
        "https://example.com/",
        "https://another.io/about",
        "https://third.ai/",
    ]


@pytest.mark.asyncio
async def test_search_respects_limit() -> None:
    transport = DDGMockTransport(body=DDG_RESULTS_HTML)
    http_client, search = _make_client(transport)
    try:
        results = await search.search("query", limit=1)
    finally:
        await http_client.aclose()
    assert len(results) == 1
    assert results[0] == "https://example.com/"


# ---------------------------------------------------------------------------
# Throttle: two consecutive calls wait at least seconds_between_requests apart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_throttle_enforced() -> None:
    """Two consecutive searches wait at least seconds_between_requests apart."""
    throttle = 0.1  # 100ms — short enough to not slow the test suite much
    transport = DDGMockTransport(body=DDG_RESULTS_HTML)
    http_client = httpx.AsyncClient(transport=transport, follow_redirects=True)
    search = DuckDuckGoSearch(
        http_client,
        user_agent=USER_AGENT,
        seconds_between_requests=throttle,
    )
    try:
        await search.search("first query")
        t1 = time.monotonic()
        await search.search("second query")
        t2 = time.monotonic()
    finally:
        await http_client.aclose()

    gap = t2 - t1
    assert gap >= throttle * 0.9, (
        f"Gap between requests {gap:.4f}s < expected {throttle:.4f}s — throttle not enforced"
    )


# ---------------------------------------------------------------------------
# is_aggregator
# ---------------------------------------------------------------------------


def test_is_aggregator_exact_match() -> None:
    assert is_aggregator("https://linkedin.com/company/foo") is True


def test_is_aggregator_www_prefix() -> None:
    assert is_aggregator("https://www.linkedin.com/company/foo") is True


def test_is_aggregator_subdomain() -> None:
    assert is_aggregator("https://foo.linkedin.com/") is True


def test_is_aggregator_non_aggregator() -> None:
    assert is_aggregator("https://example.com/") is False


def test_is_aggregator_sec_gov() -> None:
    assert is_aggregator("https://www.sec.gov/cgi-bin/browse-edgar") is True


def test_is_aggregator_crunchbase() -> None:
    assert is_aggregator("https://crunchbase.com/organization/acme") is True


def test_is_aggregator_non_aggregator_deep_path() -> None:
    assert is_aggregator("https://mycompany.io/about/team") is False


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class SequencedTransport(httpx.AsyncBaseTransport):
    """Returns scripted (status, body) responses in order, repeating the last."""

    def __init__(self, script: list[tuple[int, str]]) -> None:
        self._script = script
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        idx = min(self.call_count, len(self._script) - 1)
        self.call_count += 1
        status, body = self._script[idx]
        return httpx.Response(
            status,
            content=body.encode(),
            headers={"content-type": "text/html"},
        )


@pytest.mark.asyncio
async def test_breaker_opens_after_consecutive_blocked_responses() -> None:
    """After 5 consecutive blocked responses (DDG 202 rate limit), the breaker
    opens: no further HTTP requests are made and search returns []."""
    transport = SequencedTransport([(202, "")])
    http_client, search = _make_client(transport)  # type: ignore[arg-type]

    for _ in range(5):
        assert await search.search("query") == []
    assert transport.call_count == 5
    assert search.is_blocked is True

    # Breaker open: the next search short-circuits without a request.
    assert await search.search("query") == []
    assert transport.call_count == 5
    await http_client.aclose()


@pytest.mark.asyncio
async def test_clean_response_resets_breaker_counter() -> None:
    """A successful 200 response resets the consecutive-blocked counter."""
    script = [(202, "")] * 4 + [(200, DDG_RESULTS_HTML)] + [(202, "")] * 4
    transport = SequencedTransport(script)
    http_client, search = _make_client(transport)  # type: ignore[arg-type]

    for _ in range(9):
        await search.search("query")

    # 4 blocked + reset + 4 blocked — never 5 consecutive, breaker closed.
    assert search.is_blocked is False
    assert transport.call_count == 9
    await http_client.aclose()


@pytest.mark.asyncio
async def test_captcha_body_counts_toward_breaker() -> None:
    """A 200 response carrying the anti-bot interstitial counts as blocked."""
    transport = SequencedTransport([(200, DDG_CAPTCHA_HTML)])
    http_client, search = _make_client(transport)  # type: ignore[arg-type]

    for _ in range(5):
        assert await search.search("query") == []
    assert search.is_blocked is True
    await http_client.aclose()
