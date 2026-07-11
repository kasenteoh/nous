"""Tests for nous.sources.geekwire.

Same mock-transport pattern as test_siliconangle.py — no real network calls.
The fixture is a trimmed capture of the live /tag/funding/feed/ (2026-07).
"""

from __future__ import annotations

from pathlib import Path

import httpx

from nous.sources.geekwire import (
    GEEKWIRE_FUNDING_FEED,
    SNIPPET_MAX_CHARS,
    fetch_geekwire_funding_articles,
)
from nous.sources.news import NewsArticleResult, NewsClient

FIXTURES = Path(__file__).parent / "fixtures"
GW_XML = (FIXTURES / "geekwire_sample.xml").read_text()

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


def _routes(*, feed_status: int = 200, feed_body: str = GW_XML) -> list[_Route]:
    return [
        _Route("geekwire.com/robots.txt", status=404),
        _Route("geekwire.com/tag/funding/feed", status=feed_status, body=feed_body),
    ]


def test_geekwire_feed_url_constant() -> None:
    """Pin the funding-tag feed URL — the adapter's whole contract.

    Deliberately the /tag/funding/ feed, not /fundings/ (an HTML tracker page
    with no feed) and not the broad main feed.
    """
    assert GEEKWIRE_FUNDING_FEED == "https://www.geekwire.com/tag/funding/feed/"


# ---------------------------------------------------------------------------
# Canary: fixture parse floor + well-formed fields
# ---------------------------------------------------------------------------


async def test_geekwire_canary_parses_fixture_entries() -> None:
    """The funding-tag fixture must yield >= 10 well-formed entries."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes()))
        results = await fetch_geekwire_funding_articles(client, lookback_days=-1)

    assert len(results) >= 10, (
        f"Expected >=10 entries from the funding-tag fixture, got {len(results)}"
    )
    for r in results:
        assert isinstance(r, NewsArticleResult)
        assert r.url.startswith("https://www.geekwire.com/")
        assert r.title.strip()
        assert r.source == "geekwire.com"
        assert r.published_date is not None
        assert len(r.raw_content) <= SNIPPET_MAX_CHARS

    urls = {r.url for r in results}
    assert any("kredosai" in u for u in urls), "KredosAI $7M item missing"


async def test_geekwire_keeps_no_keyword_funding_titles() -> None:
    """The tag is the filter: genuine items whose titles carry no
    FUNDING_KEYWORDS hit (e.g. 'emerge from stealth with $46M') must be kept —
    this is why the adapter passes require_keywords=False."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes()))
        results = await fetch_geekwire_funding_articles(client, lookback_days=-1)

    urls = {r.url for r in results}
    assert any("emerge-from-stealth" in u or "biotech" in u for u in urls), (
        "keyword-less funding item was dropped — require_keywords must stay False"
    )


async def test_geekwire_applies_lookback_filtering() -> None:
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>GeekWire</title>
      <item>
        <title>Old startup raised $5M</title>
        <link>https://www.geekwire.com/2020/old-funding-item</link>
        <pubDate>Mon, 06 Jan 2020 12:00:00 +0000</pubDate>
        <description>Ancient news.</description>
      </item>
      <item>
        <title>Fresh startup raises $12M</title>
        <link>https://www.geekwire.com/2026/fresh-funding-item</link>
        <pubDate>Sat, 13 Jun 2026 00:00:00 +0000</pubDate>
        <description>Recent news.</description>
      </item>
    </channel></rss>
    """
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes(feed_body=rss)))
        tight = await fetch_geekwire_funding_articles(client, lookback_days=7)
        wide = await fetch_geekwire_funding_articles(client, lookback_days=10000)

    assert "https://www.geekwire.com/2020/old-funding-item" not in {r.url for r in tight}
    assert {r.url for r in wide} == {
        "https://www.geekwire.com/2020/old-funding-item",
        "https://www.geekwire.com/2026/fresh-funding-item",
    }


# ---------------------------------------------------------------------------
# Error handling: robots block / HTTP error / network failure => []
# ---------------------------------------------------------------------------


async def test_geekwire_robots_block_returns_empty() -> None:
    routes = [
        _Route("geekwire.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL),
        _Route("geekwire.com/tag/funding/feed", status=200, body=GW_XML),
    ]
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(routes))
        results = await fetch_geekwire_funding_articles(client)

    assert results == []
    assert routes[1].call_count == 0, "feed must not be fetched under a robots block"


async def test_geekwire_http_error_returns_empty() -> None:
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes(feed_status=500)))
        results = await fetch_geekwire_funding_articles(client)
    assert results == []


async def test_geekwire_network_error_returns_empty() -> None:
    routes = [
        _Route("geekwire.com/robots.txt", status=404),
        _Route("geekwire.com/tag/funding/feed", raise_network_error=True),
    ]
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(routes))
        results = await fetch_geekwire_funding_articles(client)
    assert results == []
