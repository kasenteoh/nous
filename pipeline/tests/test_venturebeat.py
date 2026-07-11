"""Tests for nous.sources.venturebeat.

Same mock-transport pattern as test_siliconangle.py — no real network calls.
The fixture is a trimmed capture of the live main feed (2026-07) with the two
real funding items (Railway, Listen Labs) that sat in VB's category-feed
window at capture time, so the keyword filter has both classes to bite on.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from nous.sources.news import NewsArticleResult, NewsClient
from nous.sources.venturebeat import (
    SNIPPET_MAX_CHARS,
    VENTUREBEAT_FEED,
    fetch_venturebeat_funding_articles,
)

FIXTURES = Path(__file__).parent / "fixtures"
VB_XML = (FIXTURES / "venturebeat_sample.xml").read_text()

USER_AGENT = "nous-test test@example.com"

ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


class _Route:
    def __init__(
        self,
        substring: str,
        *,
        status: int = 200,
        body: str = "",
        content_type: str = "text/html",
        raise_network_error: bool = False,
    ) -> None:
        self.substring = substring
        self.status = status
        self.body = body
        self.content_type = content_type
        self.raise_network_error = raise_network_error
        self.call_count = 0


class _MockTransport(httpx.AsyncBaseTransport):
    """Dispatches to first matching route; 404 by default."""

    def __init__(self, routes: list[_Route]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for r in self._routes:
            if r.substring in url_str:
                r.call_count += 1
                if r.raise_network_error:
                    raise httpx.ConnectError("Connection refused")
                resp = httpx.Response(
                    r.status,
                    content=r.body.encode(),
                    headers={"content-type": r.content_type},
                )
                if r.status >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {r.status}", request=request, response=resp
                    )
                return resp
        return httpx.Response(404, content=b"Not Found")


def _inject(client: NewsClient, transport: httpx.AsyncBaseTransport) -> None:
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


def _routes(*, feed_status: int = 200, feed_body: str = VB_XML) -> list[_Route]:
    return [
        _Route("venturebeat.com/robots.txt", status=404),
        _Route("venturebeat.com/feed", status=feed_status, body=feed_body),
    ]


def test_venturebeat_feed_url_constant() -> None:
    """Pin the canonical feed URL (the /feed/ variant 308-redirects here)."""
    assert VENTUREBEAT_FEED == "https://venturebeat.com/feed"


# ---------------------------------------------------------------------------
# Canary: fixture parse floor + well-formed fields
# ---------------------------------------------------------------------------


async def test_venturebeat_canary_parses_fixture_funding_entries() -> None:
    """The fixture must yield the two funding items with well-formed fields."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes()))
        results = await fetch_venturebeat_funding_articles(client, lookback_days=-1)

    assert len(results) >= 2, (
        f"Expected >=2 funding-keyword hits in the fixture, got {len(results)}"
    )
    for r in results:
        assert isinstance(r, NewsArticleResult)
        assert r.url.startswith("https://venturebeat.com/")
        assert r.title.strip()
        assert r.source == "venturebeat.com"
        assert r.published_date is not None
        assert len(r.raw_content) <= SNIPPET_MAX_CHARS

    urls = {r.url for r in results}
    assert any("railway-secures" in u for u in urls), "Railway $100M item missing"
    assert any("listen-labs-raises" in u for u in urls), "Listen Labs $69M item missing"


async def test_venturebeat_drops_non_funding_items() -> None:
    """Editorial/research pieces without funding keywords must be filtered."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes()))
        results = await fetch_venturebeat_funding_articles(client, lookback_days=-1)

    urls = {r.url for r in results}
    assert not any("confidently-wrong" in u for u in urls), (
        "AI-agents research piece (no funding signal) leaked through the filter"
    )
    assert not any("shared-api-keys" in u for u in urls), (
        "Security research piece (no funding signal) leaked through the filter"
    )


async def test_venturebeat_truncates_full_body_descriptions() -> None:
    """VB descriptions are full article bodies; raw_content must stay bounded
    while the keyword filter still sees the full text (keyword only deep in
    the description)."""
    long_tail = "word " * 400  # pushes the keyword position past the cap
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>VentureBeat</title>
      <item>
        <title>Acme lands new capital to expand</title>
        <link>https://venturebeat.com/acme-lands-capital</link>
        <pubDate>Sat, 13 Jun 2026 12:00:00 +0000</pubDate>
        <description>{long_tail} The round was led by Example Ventures funding.</description>
      </item>
    </channel></rss>
    """
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes(feed_body=rss)))
        results = await fetch_venturebeat_funding_articles(client, lookback_days=-1)

    assert len(results) == 1, "keyword beyond the cap must still match (filter pre-truncation)"
    assert len(results[0].raw_content) == SNIPPET_MAX_CHARS


# ---------------------------------------------------------------------------
# Error handling: robots block / HTTP error / network failure => []
# ---------------------------------------------------------------------------


async def test_venturebeat_robots_block_returns_empty() -> None:
    routes = [
        _Route("venturebeat.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL),
        _Route("venturebeat.com/feed", status=200, body=VB_XML),
    ]
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(routes))
        results = await fetch_venturebeat_funding_articles(client)

    assert results == []
    feed_route = routes[1]
    assert feed_route.call_count == 0, "feed must not be fetched under a robots block"


async def test_venturebeat_http_error_returns_empty() -> None:
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes(feed_status=403)))
        results = await fetch_venturebeat_funding_articles(client)
    assert results == []


async def test_venturebeat_network_error_returns_empty() -> None:
    routes = [
        _Route("venturebeat.com/robots.txt", status=404),
        _Route("venturebeat.com/feed", raise_network_error=True),
    ]
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(routes))
        results = await fetch_venturebeat_funding_articles(client)
    assert results == []
