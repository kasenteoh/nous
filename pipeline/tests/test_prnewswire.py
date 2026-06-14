"""Tests for nous.sources.prnewswire.

Same mock-transport pattern as test_news.py — no real network calls.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from nous.sources.news import NewsArticleResult, NewsClient, _matches_funding_keyword
from nous.sources.prnewswire import PRNEWSWIRE_VC_FEED, fetch_prnewswire_funding_articles

FIXTURES = Path(__file__).parent / "fixtures"
PRN_XML = (FIXTURES / "prnewswire_sample.xml").read_text()

USER_AGENT = "nous-test test@example.com"

ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


# ---------------------------------------------------------------------------
# Transport helper — keyed by URL substring → (status, body, content_type).
# Copied from test_news.py so this test file is self-contained.
# ---------------------------------------------------------------------------


class _Route:
    def __init__(
        self,
        substring: str,
        *,
        status: int = 200,
        body: str = "",
        content_type: str = "text/xml",
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


def test_prnewswire_feed_url_constant() -> None:
    """Pin the PR Newswire feed URL — adapter has no other knob, so this is the contract."""
    assert PRNEWSWIRE_VC_FEED == (
        "https://www.prnewswire.com/rss/financial-services-latest-news/venture-capital-list.rss"
    )


# ---------------------------------------------------------------------------
# Happy path — parse fixture
# ---------------------------------------------------------------------------


async def test_prnewswire_adapter_returns_entries_from_fixture() -> None:
    """Parsing the fixture yields NewsArticleResult items with all required fields."""
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", status=200, body=PRN_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        # lookback_days=-1 disables the date cutoff so the captured fixture
        # (which ages out as wall-clock time advances) keeps yielding hits.
        results = await fetch_prnewswire_funding_articles(client, lookback_days=-1)

    assert len(results) > 0, "Expected at least one funding-keyword hit in the fixture"
    # All returned results must be valid NewsArticleResult instances.
    for r in results:
        assert isinstance(r, NewsArticleResult)
        assert r.url.startswith("https://www.prnewswire.com/")
        assert r.title
        assert r.source  # hostname populated
        assert r.raw_content or r.raw_content == ""  # may be empty if no summary


async def test_prnewswire_adapter_fields_are_populated() -> None:
    """url, title, source, published_date, and raw_content are all set."""
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", status=200, body=PRN_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_prnewswire_funding_articles(client, lookback_days=-1)

    assert results, "Expected results from fixture"
    sample = results[0]
    assert sample.url.startswith("http")
    assert sample.title
    # canonical_url() strips leading www., so the source hostname is normalised.
    assert "prnewswire.com" in sample.source
    # published_date should be a date object (not None) for well-formed items.
    assert sample.published_date is not None
    # raw_content holds the HTML-stripped description snippet.
    assert isinstance(sample.raw_content, str)


# ---------------------------------------------------------------------------
# Funding-keyword filtering
# ---------------------------------------------------------------------------


async def test_prnewswire_adapter_keeps_funding_items() -> None:
    """Items with funding keywords survive the filter."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Venture Capital</title>
      <item>
        <title>Acme raises $50M Series A led by Sequoia</title>
        <link>https://www.prnewswire.com/news-releases/acme-funding-123.html</link>
        <guid>https://www.prnewswire.com/news-releases/acme-funding-123.html</guid>
        <pubDate>Mon, 26 May 2026 12:00:00 +0000</pubDate>
        <description><![CDATA[<p>Acme today announced a funding round.</p>]]></description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", status=200, body=rss),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_prnewswire_funding_articles(client, lookback_days=-1)

    assert len(results) == 1
    assert "acme-funding" in results[0].url


async def test_prnewswire_adapter_drops_non_funding_items() -> None:
    """Items without any funding keyword are excluded."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Venture Capital</title>
      <item>
        <title>Acme Appoints New Chief Marketing Officer</title>
        <link>https://www.prnewswire.com/news-releases/acme-cmo-456.html</link>
        <guid>https://www.prnewswire.com/news-releases/acme-cmo-456.html</guid>
        <pubDate>Mon, 26 May 2026 12:00:00 +0000</pubDate>
        <description><![CDATA[<p>Acme announces leadership change.</p>]]></description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", status=200, body=rss),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_prnewswire_funding_articles(client, lookback_days=-1)

    assert results == []


async def test_prnewswire_adapter_all_fixture_results_match_keyword() -> None:
    """Every result from the fixture must contain a funding keyword."""
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", status=200, body=PRN_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_prnewswire_funding_articles(client, lookback_days=-1)

    for r in results:
        assert _matches_funding_keyword(f"{r.title}\n{r.raw_content}"), (
            f"Entry survived keyword filter without a match: {r.title}"
        )


# ---------------------------------------------------------------------------
# Lookback filtering
# ---------------------------------------------------------------------------


async def test_prnewswire_adapter_applies_lookback_window() -> None:
    """Entries older than lookback_days are dropped."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Venture Capital</title>
      <item>
        <title>Old Startup raises $10M seed round</title>
        <link>https://www.prnewswire.com/news-releases/old-funding-001.html</link>
        <guid>https://www.prnewswire.com/news-releases/old-funding-001.html</guid>
        <pubDate>Mon, 06 Jan 2020 12:00:00 +0000</pubDate>
        <description><![CDATA[<p>An ancient seed funding announcement.</p>]]></description>
      </item>
      <item>
        <title>Recent Corp raises $50M Series B led by Acme Capital</title>
        <link>https://www.prnewswire.com/news-releases/recent-funding-002.html</link>
        <guid>https://www.prnewswire.com/news-releases/recent-funding-002.html</guid>
        <pubDate>Mon, 26 May 2026 12:00:00 +0000</pubDate>
        <description><![CDATA[<p>Recent Series B funding round details.</p>]]></description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", status=200, body=rss),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        # Very long window — both dates survive.
        long_window = await fetch_prnewswire_funding_articles(client, lookback_days=10000)
        # 7-day window: the 2020 entry must be excluded.
        tight_window = await fetch_prnewswire_funding_articles(client, lookback_days=7)

    long_urls = {r.url for r in long_window}
    assert any("old-funding" in u for u in long_urls)
    assert any("recent-funding" in u for u in long_urls)

    tight_urls = {r.url for r in tight_window}
    assert not any("old-funding" in u for u in tight_urls)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


async def test_prnewswire_adapter_deduplicates_by_canonical_url() -> None:
    """Two RSS items differing only in tracking params collapse to one entry."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Venture Capital</title>
      <item>
        <title>Acme raises $50M Series A</title>
        <link>https://www.prnewswire.com/news-releases/acme-funding-789.html?utm_source=rss</link>
        <guid>https://www.prnewswire.com/news-releases/acme-funding-789-v1.html</guid>
        <pubDate>Mon, 26 May 2026 12:00:00 +0000</pubDate>
        <description><![CDATA[<p>Funding round details.</p>]]></description>
      </item>
      <item>
        <title>Acme raises $50M Series A (re-post)</title>
        <link>https://www.prnewswire.com/news-releases/acme-funding-789.html?utm_source=newsletter</link>
        <guid>https://www.prnewswire.com/news-releases/acme-funding-789-v2.html</guid>
        <pubDate>Mon, 26 May 2026 13:00:00 +0000</pubDate>
        <description><![CDATA[<p>Funding round details repeated.</p>]]></description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", status=200, body=rss),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_prnewswire_funding_articles(client, lookback_days=-1)

    assert len(results) == 1
    # Both items had titles with "raises" — one passes the keyword filter.
    assert "acme-funding-789" in results[0].url


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_prnewswire_adapter_robots_block_returns_empty() -> None:
    """robots.txt disallow-all causes the adapter to return []."""
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL),
            _Route("prnewswire.com/rss/", status=200, body=PRN_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_prnewswire_funding_articles(client)

    assert results == []
    # The feed endpoint must not have been called — robots block short-circuits.
    feed_route = next(r for r in transport._routes if "rss/" in r.substring)
    assert feed_route.call_count == 0


async def test_prnewswire_adapter_handles_http_error() -> None:
    """An HTTP error on the feed (e.g. 503) returns [] without raising."""
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", status=503, body="Service Unavailable"),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_prnewswire_funding_articles(client)

    assert results == []


async def test_prnewswire_adapter_handles_network_error() -> None:
    """A network-level connection error returns [] without raising."""
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", raise_network_error=True),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_prnewswire_funding_articles(client)

    assert results == []


async def test_prnewswire_adapter_handles_empty_feed() -> None:
    """An empty (no items) feed returns [] without raising."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Venture Capital</title>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("prnewswire.com/robots.txt", status=404),
            _Route("prnewswire.com/rss/", status=200, body=rss),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_prnewswire_funding_articles(client, lookback_days=-1)

    assert results == []
