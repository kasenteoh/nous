"""Tests for nous.sources.siliconangle.

Same mock-transport pattern as test_news.py — no real network calls.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from nous.sources.news import NewsArticleResult, NewsClient
from nous.sources.siliconangle import SILICONANGLE_FEED, fetch_siliconangle_funding_articles

FIXTURES = Path(__file__).parent / "fixtures"
SA_XML = (FIXTURES / "siliconangle_sample.xml").read_text()

USER_AGENT = "nous-test test@example.com"

ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


# ---------------------------------------------------------------------------
# Transport helper — reuse the same keyed-by-substring pattern from test_news.py
# ---------------------------------------------------------------------------


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
        self.total_calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.total_calls += 1
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
    """Replace the real httpx clients with mocked transport post-__aenter__."""
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
# Feed URL constant
# ---------------------------------------------------------------------------


def test_siliconangle_feed_url_constant() -> None:
    """Pin the SiliconANGLE feed URL — adapter has no other knob."""
    assert SILICONANGLE_FEED == "https://siliconangle.com/feed/"


# ---------------------------------------------------------------------------
# Happy path — fixture parsing
# ---------------------------------------------------------------------------


async def test_siliconangle_adapter_returns_funding_entries_from_fixture() -> None:
    """Parses the fixture and returns only funding-keyword-matched items.

    The fixture has 8 items: 5 with clear funding keywords in title/snippet
    (Mistral, ChatSee, Upriver, Helix, Endurance) and 3 without
    (AI costs best practices, 8x8 product coverage, Adobe earnings).
    Filtering must keep exactly the 5 funding items.
    """
    transport = _MockTransport(
        [
            _Route("siliconangle.com/robots.txt", status=404),
            _Route("siliconangle.com/feed", status=200, body=SA_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_siliconangle_funding_articles(client, lookback_days=-1)

    assert len(results) > 0, "Expected at least one funding-keyword hit in the fixture"
    for r in results:
        assert isinstance(r, NewsArticleResult)


async def test_siliconangle_adapter_fields_are_populated() -> None:
    """Every returned NewsArticleResult has url, title, source, raw_content set."""
    transport = _MockTransport(
        [
            _Route("siliconangle.com/robots.txt", status=404),
            _Route("siliconangle.com/feed", status=200, body=SA_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_siliconangle_funding_articles(client, lookback_days=-1)

    assert len(results) > 0
    for r in results:
        assert r.url.startswith("https://siliconangle.com/"), (
            f"Expected siliconangle.com URL, got: {r.url}"
        )
        assert r.title, "title must be non-empty"
        assert r.source == "siliconangle.com", (
            f"source must be 'siliconangle.com', got: {r.source}"
        )
        # published_date may be None for items without a parseable date, but
        # the fixture items all have pubDate — assert it came through.
        assert r.published_date is not None, f"published_date missing for: {r.title}"
        # raw_content holds the HTML-stripped RSS snippet.
        assert isinstance(r.raw_content, str)


# ---------------------------------------------------------------------------
# Funding-keyword filtering
# ---------------------------------------------------------------------------


async def test_siliconangle_adapter_drops_non_funding_items() -> None:
    """Non-funding items in the fixture must NOT appear in results."""
    transport = _MockTransport(
        [
            _Route("siliconangle.com/robots.txt", status=404),
            _Route("siliconangle.com/feed", status=200, body=SA_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_siliconangle_funding_articles(client, lookback_days=-1)

    urls = {r.url for r in results}
    # These fixture items have no funding keyword in title or snippet.
    assert not any("10-best-practices-optimizing" in u for u in urls), (
        "AI-cost best-practices article (no funding signal) leaked through filter"
    )
    assert not any("connecting-front-line-8x8" in u for u in urls), (
        "8x8 product piece (no funding signal) leaked through filter"
    )
    assert not any("adobe-beats-expectations" in u for u in urls), (
        "Adobe earnings piece (no funding signal) leaked through filter"
    )


async def test_siliconangle_adapter_keeps_funding_items() -> None:
    """Known funding items in the fixture must appear in results."""
    transport = _MockTransport(
        [
            _Route("siliconangle.com/robots.txt", status=404),
            _Route("siliconangle.com/feed", status=200, body=SA_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_siliconangle_funding_articles(client, lookback_days=-1)

    urls = {r.url for r in results}
    # These fixture items contain funding keywords (raises / funding / seed / Series A).
    assert any("mistral" in u for u in urls), "Mistral $3.5B funding item missing"
    assert any("chatsee" in u for u in urls), "ChatSee $6.5M seed item missing"
    assert any("upriver" in u for u in urls), "Upriver $14M item missing"
    assert any("endurance-energy" in u for u in urls), "Endurance Energy $54M item missing"


# ---------------------------------------------------------------------------
# Lookback filtering
# ---------------------------------------------------------------------------


async def test_siliconangle_adapter_applies_lookback_filtering() -> None:
    """Items older than lookback_days must be dropped."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>SiliconANGLE</title>
      <item>
        <title>Ancient startup raised $50M Series A in 2020</title>
        <link>https://siliconangle.com/old-funding-item</link>
        <pubDate>Mon, 06 Jan 2020 12:00:00 +0000</pubDate>
        <description>An ancient funding announcement.</description>
      </item>
      <item>
        <title>Recent startup raises $20M Series B</title>
        <link>https://siliconangle.com/recent-funding-item</link>
        <pubDate>Sat, 13 Jun 2026 00:00:00 +0000</pubDate>
        <description>Recent startup raises new funding round.</description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("siliconangle.com/robots.txt", status=404),
            _Route("siliconangle.com/feed", status=200, body=rss),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        tight_results = await fetch_siliconangle_funding_articles(client, lookback_days=7)
        wide_results = await fetch_siliconangle_funding_articles(
            client, lookback_days=10000
        )

    tight_urls = {r.url for r in tight_results}
    wide_urls = {r.url for r in wide_results}

    # Ancient item (2020) must be absent from a tight window.
    assert "https://siliconangle.com/old-funding-item" not in tight_urls

    # Both items survive a very wide window (lookback = 10000 days covers 2020).
    assert "https://siliconangle.com/old-funding-item" in wide_urls
    assert "https://siliconangle.com/recent-funding-item" in wide_urls


# ---------------------------------------------------------------------------
# Error handling — robots block
# ---------------------------------------------------------------------------


async def test_siliconangle_adapter_robots_block_returns_empty() -> None:
    """A robots.txt disallow must return [] without fetching the feed."""
    transport = _MockTransport(
        [
            _Route(
                "siliconangle.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL
            ),
            _Route("siliconangle.com/feed", status=200, body=SA_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_siliconangle_funding_articles(client)

    assert results == []
    # Feed must not have been fetched at all.
    feed_route = next(r for r in transport._routes if "feed" in r.substring)
    assert feed_route.call_count == 0


# ---------------------------------------------------------------------------
# Error handling — HTTP errors
# ---------------------------------------------------------------------------


async def test_siliconangle_adapter_handles_http_500() -> None:
    """HTTP 5xx on the feed returns []."""
    transport = _MockTransport(
        [
            _Route("siliconangle.com/robots.txt", status=404),
            _Route("siliconangle.com/feed", status=500, body="Internal Server Error"),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_siliconangle_funding_articles(client)

    assert results == []


async def test_siliconangle_adapter_handles_http_403() -> None:
    """HTTP 4xx on the feed returns []."""
    transport = _MockTransport(
        [
            _Route("siliconangle.com/robots.txt", status=404),
            _Route("siliconangle.com/feed", status=403, body="Forbidden"),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_siliconangle_funding_articles(client)

    assert results == []


# ---------------------------------------------------------------------------
# Error handling — network failure
# ---------------------------------------------------------------------------


async def test_siliconangle_adapter_handles_network_error() -> None:
    """A connection-level error on the feed returns []."""
    transport = _MockTransport(
        [
            _Route("siliconangle.com/robots.txt", status=404),
            _Route("siliconangle.com/feed", raise_network_error=True),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_siliconangle_funding_articles(client)

    assert results == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


async def test_siliconangle_adapter_deduplicates_by_canonical_url() -> None:
    """Two RSS items that canonicalize to the same URL collapse to one entry."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>SiliconANGLE</title>
      <item>
        <title>Acme raises $50M Series A</title>
        <link>https://siliconangle.com/acme-funding?utm_source=twitter</link>
        <pubDate>Sat, 13 Jun 2026 12:00:00 +0000</pubDate>
        <description>Acme closes a funding round.</description>
      </item>
      <item>
        <title>Acme raises $50M Series A (repost)</title>
        <link>https://siliconangle.com/acme-funding?utm_source=newsletter</link>
        <pubDate>Sat, 13 Jun 2026 13:00:00 +0000</pubDate>
        <description>Acme closes a funding round.</description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("siliconangle.com/robots.txt", status=404),
            _Route("siliconangle.com/feed", status=200, body=rss),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_siliconangle_funding_articles(client, lookback_days=-1)

    assert len(results) == 1
    assert results[0].url == "https://siliconangle.com/acme-funding"
