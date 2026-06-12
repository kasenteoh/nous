"""Tests for nous.sources.news + nous.sources.techcrunch.

Same mock-transport pattern as test_homepage.py — no real network calls.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nous.sources.news import (
    FUNDING_KEYWORDS,
    MIN_BODY_CHARS,
    NewsArticleResult,
    NewsClient,
    RobotsBlockedError,
    _extract_article_text,
    _matches_funding_keyword,
)
from nous.sources.techcrunch import TC_FUNDING_FEED, fetch_techcrunch_funding_articles

FIXTURES = Path(__file__).parent / "fixtures"
GOOGLE_NEWS_XML = (FIXTURES / "google_news_sample.xml").read_text()
TC_XML = (FIXTURES / "techcrunch_venture.xml").read_text()
TC_ARTICLE_HTML = (FIXTURES / "techcrunch_article.html").read_text()

USER_AGENT = "nous-test test@example.com"

# Disallow all under news.google.com — used to test robots-block on Google News.
ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


# ---------------------------------------------------------------------------
# Transport helper — keyed by URL substring → (status, body, content_type).
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


def _inject(client: NewsClient, transport: _MockTransport) -> None:
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
# Constructor validation
# ---------------------------------------------------------------------------


def test_empty_user_agent_raises() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        NewsClient(user_agent="")


def test_whitespace_user_agent_raises() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        NewsClient(user_agent="   ")


# ---------------------------------------------------------------------------
# Keyword filter (unit)
# ---------------------------------------------------------------------------


def test_matches_funding_keyword_positive() -> None:
    assert _matches_funding_keyword("Acme raises $50M Series A")
    assert _matches_funding_keyword("New funding round closed last week")
    assert _matches_funding_keyword("Round led by Sequoia")
    assert _matches_funding_keyword("Closes at $1B valuation")


def test_matches_funding_keyword_negative() -> None:
    # No funding-related word.
    assert not _matches_funding_keyword("Acme launches new product")
    assert not _matches_funding_keyword("CEO interview about market trends")


def test_funding_keywords_includes_basics() -> None:
    # Sanity guard against accidental list edits dropping core signals.
    for required in ("raised", "funding", "valuation", "series a"):
        assert required in FUNDING_KEYWORDS


# ---------------------------------------------------------------------------
# Article text extraction (unit)
# ---------------------------------------------------------------------------


def test_extract_article_text_strips_scripts_and_nav() -> None:
    html = """
    <html><body>
      <nav>Skip to content | Subscribe | Login</nav>
      <header>Site banner that should not appear</header>
      <script>var x = 1;</script>
      <style>.hide{}</style>
      <main><p>This is the real article body content.</p></main>
      <footer>Copyright 2026 SiteName</footer>
    </body></html>
    """
    text = _extract_article_text(html)
    assert "real article body" in text
    assert "Skip to content" not in text
    assert "Site banner" not in text
    assert "Copyright 2026" not in text
    assert "var x" not in text


def test_extract_article_text_collapses_whitespace() -> None:
    html = "<html><body><p>foo</p>\n\n\n<p>   bar   </p></body></html>"
    assert _extract_article_text(html) == "foo bar"


def test_extract_real_techcrunch_article_meets_min_chars() -> None:
    """The captured TC article fixture must yield more than MIN_BODY_CHARS of text."""
    text = _extract_article_text(TC_ARTICLE_HTML)
    assert len(text) > MIN_BODY_CHARS
    # Sanity: the funding claim from the headline must survive extraction.
    assert "Stord" in text
    assert "$250" in text or "250 million" in text.lower()


# ---------------------------------------------------------------------------
# google_news_rss — happy path against the captured fixture
# ---------------------------------------------------------------------------


async def test_google_news_rss_returns_keyword_matches() -> None:
    transport = _MockTransport(
        [
            _Route("news.google.com/robots.txt", status=404),
            _Route("news.google.com/rss/search", status=200, body=GOOGLE_NEWS_XML),
        ]
    )

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        # lookback_days=-1 disables the date cutoff so the captured fixture
        # (which ages out as wall-clock time advances) keeps yielding hits.
        results = await client.google_news_rss('"OpenAI" funding', lookback_days=-1)

    assert len(results) > 0, "Expected at least one funding-keyword hit in the fixture"
    # Every returned entry must mention at least one funding keyword in
    # title + snippet (the filter contract).
    for r in results:
        assert _matches_funding_keyword(f"{r.title}\n{r.raw_content}"), (
            f"Entry survived keyword filter without a match: {r.title}"
        )
    # Pydantic model assertions: required fields are present + typed.
    sample = results[0]
    assert isinstance(sample, NewsArticleResult)
    assert sample.url.startswith("http")
    assert sample.title
    assert sample.source  # hostname populated


async def test_google_news_rss_filters_out_non_funding_entries() -> None:
    """An RSS with mixed funding + non-funding entries returns only funding ones."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Test feed</title>
      <item>
        <title>Acme raises $50M Series A</title>
        <link>https://example.com/acme-funding</link>
        <pubDate>Mon, 26 May 2026 12:00:00 +0000</pubDate>
        <description>Acme today announced a funding round led by Sequoia.</description>
      </item>
      <item>
        <title>Acme launches new product line</title>
        <link>https://example.com/acme-product</link>
        <pubDate>Mon, 26 May 2026 13:00:00 +0000</pubDate>
        <description>Acme expands into new market segment.</description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("news.google.com/robots.txt", status=404),
            _Route("news.google.com/rss/search", status=200, body=rss),
        ]
    )

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await client.google_news_rss("Acme", lookback_days=-1)

    urls = {r.url for r in results}
    assert any("acme-funding" in u for u in urls)
    assert not any("acme-product" in u for u in urls)


async def test_google_news_rss_deduplicates_by_canonical_url() -> None:
    """Two RSS items differing only in tracking params collapse to one entry."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Test feed</title>
      <item>
        <title>Acme raises $50M Series A</title>
        <link>https://example.com/acme?utm_source=twitter</link>
        <pubDate>Mon, 26 May 2026 12:00:00 +0000</pubDate>
        <description>Funding round details.</description>
      </item>
      <item>
        <title>Acme raises $50M Series A (re-post)</title>
        <link>https://example.com/acme?utm_source=newsletter&amp;utm_medium=email</link>
        <pubDate>Mon, 26 May 2026 13:00:00 +0000</pubDate>
        <description>Funding round details.</description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("news.google.com/robots.txt", status=404),
            _Route("news.google.com/rss/search", status=200, body=rss),
        ]
    )

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await client.google_news_rss("Acme", lookback_days=-1)

    assert len(results) == 1
    assert results[0].url == "https://example.com/acme"


async def test_google_news_rss_robots_block_returns_empty() -> None:
    """robots.txt disallowing /rss/search must produce an empty result, not raise."""
    transport = _MockTransport(
        [
            _Route("news.google.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL),
            _Route("news.google.com/rss/search", status=200, body=GOOGLE_NEWS_XML),
        ]
    )

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await client.google_news_rss("anything", lookback_days=-1)

    assert results == []
    # Critically: the RSS endpoint must NOT have been hit when robots blocks.
    rss_route = next(
        r for r in transport._routes if "rss/search" in r.substring
    )
    assert rss_route.call_count == 0


async def test_google_news_rss_applies_lookback_window() -> None:
    """Entries older than lookback_days are dropped."""
    # One fresh-ish, one ancient. Use 2020 for ancient — well past any
    # reasonable lookback window.
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Test feed</title>
      <item>
        <title>Old funding news raised something</title>
        <link>https://example.com/old</link>
        <pubDate>Mon, 06 Jan 2020 12:00:00 +0000</pubDate>
        <description>Ancient funding announcement.</description>
      </item>
      <item>
        <title>Recent: Acme raised $50M Series A</title>
        <link>https://example.com/recent</link>
        <pubDate>Mon, 26 May 2026 12:00:00 +0000</pubDate>
        <description>Recent funding.</description>
      </item>
    </channel></rss>
    """
    transport = _MockTransport(
        [
            _Route("news.google.com/robots.txt", status=404),
            _Route("news.google.com/rss/search", status=200, body=rss),
        ]
    )

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        # Very long window — both survive.
        long_window = await client.google_news_rss("Acme", lookback_days=10000)
        # 7-day window measured from "now" (the test wall-clock): only the
        # recent entry might survive, but since "recent" is also pinned to
        # 2026-05-26 we can't rely on time-relative behavior here. We just
        # assert the OLD entry is gone with a tight window.
        tight_window = await client.google_news_rss("Acme", lookback_days=7)

    long_urls = {r.url for r in long_window}
    assert "https://example.com/old" in long_urls
    assert "https://example.com/recent" in long_urls

    tight_urls = {r.url for r in tight_window}
    assert "https://example.com/old" not in tight_urls


# ---------------------------------------------------------------------------
# fetch_article_body
# ---------------------------------------------------------------------------


async def test_fetch_article_body_returns_clean_text() -> None:
    transport = _MockTransport(
        [
            _Route("techcrunch.com/robots.txt", status=404),
            _Route("techcrunch.com/2026", status=200, body=TC_ARTICLE_HTML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        body = await client.fetch_article_body(
            "https://techcrunch.com/2026/05/26/amazon-fulfillment-competitor-stord-raises-250m-at-3b-valuation/"
        )

    assert body is not None
    assert len(body) >= MIN_BODY_CHARS
    assert "Stord" in body
    # Script tags must have been stripped.
    assert "<script" not in body.lower()


async def test_fetch_article_body_returns_none_on_robots_block() -> None:
    transport = _MockTransport(
        [
            _Route("paywall.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL),
            _Route("paywall.com/article", status=200, body=TC_ARTICLE_HTML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        body = await client.fetch_article_body("https://paywall.com/article/x")

    assert body is None


async def test_fetch_article_body_returns_none_on_404() -> None:
    transport = _MockTransport(
        [
            _Route("example.com/robots.txt", status=404),
            _Route("example.com/missing", status=404, body="not found"),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        body = await client.fetch_article_body("https://example.com/missing")

    assert body is None


async def test_fetch_article_body_returns_none_on_500() -> None:
    transport = _MockTransport(
        [
            _Route("example.com/robots.txt", status=404),
            _Route("example.com/oops", status=500, body="server error"),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        body = await client.fetch_article_body("https://example.com/oops")

    assert body is None


async def test_fetch_article_body_returns_none_on_short_body() -> None:
    """A page below MIN_BODY_CHARS of extracted text returns None (paywall stub)."""
    short_html = "<html><body><p>Subscribe to read this article.</p></body></html>"
    transport = _MockTransport(
        [
            _Route("paywall.com/robots.txt", status=404),
            _Route("paywall.com/article", status=200, body=short_html),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        body = await client.fetch_article_body("https://paywall.com/article/x")

    assert body is None


async def test_fetch_article_body_returns_none_on_network_error() -> None:
    transport = _MockTransport(
        [
            _Route("badhost.com/robots.txt", status=404),
            _Route("badhost.com/article", raise_network_error=True),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        body = await client.fetch_article_body("https://badhost.com/article")

    assert body is None


# ---------------------------------------------------------------------------
# Context-manager discipline
# ---------------------------------------------------------------------------


async def test_client_without_context_manager_raises() -> None:
    client = NewsClient(user_agent=USER_AGENT)
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.fetch_article_body("https://example.com/x")


# ---------------------------------------------------------------------------
# TechCrunch adapter
# ---------------------------------------------------------------------------


async def test_techcrunch_adapter_returns_entries_from_fixture() -> None:
    transport = _MockTransport(
        [
            _Route("techcrunch.com/robots.txt", status=404),
            _Route("techcrunch.com/category/venture/feed", status=200, body=TC_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_techcrunch_funding_articles(client, lookback_days=-1)

    assert len(results) > 0
    # TC entries don't need to match the funding keyword filter — the tag
    # itself is the filter. Sanity: every URL is on techcrunch.com.
    for r in results:
        assert r.source == "techcrunch.com"
        assert r.url.startswith("https://techcrunch.com/")


async def test_techcrunch_adapter_robots_block_returns_empty() -> None:
    transport = _MockTransport(
        [
            _Route("techcrunch.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL),
            _Route("techcrunch.com/category/venture/feed", status=200, body=TC_XML),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_techcrunch_funding_articles(client)

    assert results == []
    feed_route = next(
        r for r in transport._routes if "category/venture/feed" in r.substring
    )
    assert feed_route.call_count == 0


async def test_techcrunch_adapter_handles_http_error() -> None:
    transport = _MockTransport(
        [
            _Route("techcrunch.com/robots.txt", status=404),
            _Route("techcrunch.com/category/venture/feed", status=503, body="oops"),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await fetch_techcrunch_funding_articles(client)

    assert results == []


# ---------------------------------------------------------------------------
# RobotsBlockedError leakage — sanity
# ---------------------------------------------------------------------------


def test_robots_blocked_error_is_subclass_of_exception() -> None:
    assert issubclass(RobotsBlockedError, Exception)


def test_tc_feed_url_constant() -> None:
    """Pin the TC feed URL — adapter has no other knob, so this is the contract."""
    assert TC_FUNDING_FEED == "https://techcrunch.com/category/venture/feed/"
