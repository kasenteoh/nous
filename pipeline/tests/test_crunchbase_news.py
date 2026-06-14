"""Tests for nous.sources.crunchbase_news.

MockTransport pattern mirrors test_news.py — no real network calls.

The fixture (crunchbase_news_sample.xml) contains 8 items:
  - 4 with funding-keyword signals (funding rounds, closes, raised, led by)
  - 4 without (IPO news, editorial, layoffs tracker, market analysis)
All items are dated 2026-06-05 through 2026-06-12.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from nous.sources.crunchbase_news import CB_NEWS_FEED, fetch_crunchbase_news_funding_articles
from nous.sources.news import (
    NewsArticleResult,
    NewsClient,
    _matches_funding_keyword,
)

FIXTURES = Path(__file__).parent / "fixtures"
CB_XML = (FIXTURES / "crunchbase_news_sample.xml").read_text()

USER_AGENT = "nous-test test@example.com"
ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"

# Expected URLs of funding-signal items in the fixture. Used to assert
# that the keyword filter keeps the right items.
# Note: canonical_url (used by _parse_rss) strips trailing slashes.
FUNDING_URLS = {
    "https://news.crunchbase.com/venture/biggest-funding-rounds-ai-biotech-healthcare-ninjaone-leads",
    "https://news.crunchbase.com/venture/base10-partners-invests-real-economy-automation-funds",
    "https://news.crunchbase.com/semiconductors-and-5g/chip-startup-funding-snapshot-2026",
    "https://news.crunchbase.com/venture/biggest-funding-rounds-june-5-2026",
}

# Expected URLs of non-funding items that the keyword filter must drop.
# Note: canonical_url (used by _parse_rss) strips trailing slashes.
NON_FUNDING_URLS = {
    "https://news.crunchbase.com/public/spacex-record-breaking-ipo-spcx",
    "https://news.crunchbase.com/public/ipo-window-liquid-money-ma-schroder",
    "https://news.crunchbase.com/startups/tech-layoffs",
    "https://news.crunchbase.com/ai/bigger-acvs-bring-direct-sales-vertical-ai",
}


# ---------------------------------------------------------------------------
# Transport helper (mirrors the pattern in test_news.py)
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
    """Dispatches to the first matching route; 404 by default."""

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
    """Swap the real httpx clients with the mock transport post-__aenter__."""
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


def _make_transport(feed_status: int = 200, feed_body: str = CB_XML) -> _MockTransport:
    """Return a transport that allows robots and serves the given feed body."""
    return _MockTransport(
        [
            _Route("news.crunchbase.com/robots.txt", status=404),
            _Route(
                "news.crunchbase.com/feed",
                status=feed_status,
                body=feed_body,
                content_type="application/rss+xml",
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Feed URL constant
# ---------------------------------------------------------------------------


def test_feed_url_constant() -> None:
    """Pin the CB News feed URL — adapter has no other knob, this is the contract."""
    assert CB_NEWS_FEED == "https://news.crunchbase.com/feed/"


# ---------------------------------------------------------------------------
# Happy path: fixture parses into NewsArticleResult objects
# ---------------------------------------------------------------------------


async def test_returns_newsarticleresult_objects_from_fixture() -> None:
    """Fixture parses into typed NewsArticleResult objects with all required fields."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _make_transport())
        results = await fetch_crunchbase_news_funding_articles(client, lookback_days=-1)

    assert len(results) > 0
    sample = results[0]
    assert isinstance(sample, NewsArticleResult)
    assert sample.url.startswith("https://")
    assert sample.title
    assert sample.source  # hostname populated
    # All results must come from news.crunchbase.com
    for r in results:
        assert r.source == "news.crunchbase.com", f"Unexpected source: {r.source}"
        assert r.url.startswith("https://news.crunchbase.com/")


async def test_fixture_fields_populated() -> None:
    """Each returned article has url, title, source, published_date, raw_content."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _make_transport())
        results = await fetch_crunchbase_news_funding_articles(client, lookback_days=-1)

    assert results, "Expected at least one result from fixture"
    for r in results:
        assert r.url.startswith("https://")
        assert r.title
        assert r.source == "news.crunchbase.com"
        # published_date may be None for malformed entries, but fixture dates
        # are valid — they should all be parsed.
        assert isinstance(r.published_date, date), (
            f"Expected a date for '{r.title}', got {r.published_date!r}"
        )
        # raw_content is the stripped RSS snippet; may be empty for some items
        # but must be a str.
        assert isinstance(r.raw_content, str)


# ---------------------------------------------------------------------------
# Funding keyword filter
# ---------------------------------------------------------------------------


async def test_funding_keyword_filter_keeps_funding_items() -> None:
    """Items with funding-signal keywords in title+snippet must be returned."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _make_transport())
        results = await fetch_crunchbase_news_funding_articles(client, lookback_days=-1)

    returned_urls = {r.url for r in results}
    for expected_url in FUNDING_URLS:
        assert expected_url in returned_urls, (
            f"Funding article missing from results: {expected_url}"
        )


async def test_funding_keyword_filter_drops_non_funding_items() -> None:
    """Items without any funding keyword in title+snippet must be excluded."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _make_transport())
        results = await fetch_crunchbase_news_funding_articles(client, lookback_days=-1)

    returned_urls = {r.url for r in results}
    for non_funding_url in NON_FUNDING_URLS:
        assert non_funding_url not in returned_urls, (
            f"Non-funding article incorrectly included: {non_funding_url}"
        )


async def test_all_returned_items_match_funding_keyword() -> None:
    """Every returned entry must contain at least one FUNDING_KEYWORDS hit."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _make_transport())
        results = await fetch_crunchbase_news_funding_articles(client, lookback_days=-1)

    for r in results:
        haystack = f"{r.title}\n{r.raw_content}"
        assert _matches_funding_keyword(haystack), (
            f"Returned article has no funding keyword: {r.title!r}"
        )


# ---------------------------------------------------------------------------
# Lookback window
# ---------------------------------------------------------------------------


async def test_lookback_window_drops_old_items() -> None:
    """Items older than lookback_days are excluded from results."""
    # Fixture items span 2026-06-05 to 2026-06-12.
    # With lookback_days=3 (from 2026-06-14), only items on/after 2026-06-11 survive.
    # With lookback_days=-1, all items survive.
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Crunchbase News</title>
      <item>
        <title>Old funding raises $10M seed</title>
        <link>https://news.crunchbase.com/old-funding/</link>
        <pubDate>Mon, 06 Jan 2020 12:00:00 +0000</pubDate>
        <description>Ancient seed round.</description>
      </item>
      <item>
        <title>Recent startup raises $50M Series A</title>
        <link>https://news.crunchbase.com/recent-funding/</link>
        <pubDate>Mon, 08 Jun 2026 12:00:00 +0000</pubDate>
        <description>Recent Series A round led by Acme Ventures.</description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("news.crunchbase.com/robots.txt", status=404),
            _Route(
                "news.crunchbase.com/feed",
                status=200,
                body=rss,
                content_type="application/rss+xml",
            ),
        ]
    )

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        # lookback_days=-1 means no cutoff — both must survive the date filter
        # (keyword filter keeps "raises" / "seed" / "series a" in both titles).
        no_cutoff = await fetch_crunchbase_news_funding_articles(client, lookback_days=-1)
        tight = await fetch_crunchbase_news_funding_articles(client, lookback_days=7)

    # canonical_url strips trailing slashes
    no_cutoff_urls = {r.url for r in no_cutoff}
    assert "https://news.crunchbase.com/old-funding" in no_cutoff_urls
    assert "https://news.crunchbase.com/recent-funding" in no_cutoff_urls

    tight_urls = {r.url for r in tight}
    # The 2020 item must be gone under a 7-day window.
    assert "https://news.crunchbase.com/old-funding" not in tight_urls


# ---------------------------------------------------------------------------
# Error handling: robots block → []
# ---------------------------------------------------------------------------


async def test_robots_block_returns_empty_list() -> None:
    """A robots.txt Disallow for the feed URL must return [] without fetching the feed."""
    robots_route = _Route(
        "news.crunchbase.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL
    )
    feed_route = _Route(
        "news.crunchbase.com/feed",
        status=200,
        body=CB_XML,
        content_type="application/rss+xml",
    )
    transport = _MockTransport([robots_route, feed_route])

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_crunchbase_news_funding_articles(client)

    assert results == []
    # The feed endpoint must never have been called.
    assert feed_route.call_count == 0


# ---------------------------------------------------------------------------
# Error handling: HTTP errors → []
# ---------------------------------------------------------------------------


async def test_http_error_returns_empty_list() -> None:
    """An HTTP 503 from the feed endpoint must return [] without raising."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _make_transport(feed_status=503, feed_body="Service Unavailable"))
        results = await fetch_crunchbase_news_funding_articles(client)

    assert results == []


async def test_http_404_returns_empty_list() -> None:
    """An HTTP 404 from the feed endpoint must return [] without raising."""
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _make_transport(feed_status=404, feed_body="Not Found"))
        results = await fetch_crunchbase_news_funding_articles(client)

    assert results == []


# ---------------------------------------------------------------------------
# Error handling: network failure → []
# ---------------------------------------------------------------------------


async def test_network_error_returns_empty_list() -> None:
    """A network-level ConnectError must return [] without raising."""
    transport = _MockTransport(
        [
            _Route("news.crunchbase.com/robots.txt", status=404),
            _Route(
                "news.crunchbase.com/feed",
                raise_network_error=True,
            ),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_crunchbase_news_funding_articles(client)

    assert results == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


async def test_deduplicates_by_canonical_url() -> None:
    """Two items differing only in tracking params collapse to one result."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Crunchbase News</title>
      <item>
        <title>Acme raises $30M Series B</title>
        <link>https://news.crunchbase.com/acme/?utm_source=twitter</link>
        <pubDate>Mon, 08 Jun 2026 12:00:00 +0000</pubDate>
        <description>Series B round details.</description>
      </item>
      <item>
        <title>Acme raises $30M Series B (re-run)</title>
        <link>https://news.crunchbase.com/acme/?utm_source=newsletter</link>
        <pubDate>Mon, 08 Jun 2026 13:00:00 +0000</pubDate>
        <description>Series B round details.</description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("news.crunchbase.com/robots.txt", status=404),
            _Route(
                "news.crunchbase.com/feed",
                status=200,
                body=rss,
                content_type="application/rss+xml",
            ),
        ]
    )

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_crunchbase_news_funding_articles(client, lookback_days=-1)

    assert len(results) == 1
    # canonical_url strips trailing slashes and query params
    assert results[0].url == "https://news.crunchbase.com/acme"
