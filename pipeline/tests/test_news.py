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
    ResolvedArticle,
    RobotsBlockedError,
    _extract_article_text,
    _is_robots_exempt,
    _matches_funding_keyword,
    _phrase_in_tokens,
    _tokenize,
    article_mentions_company,
)
from nous.sources.techcrunch import TC_FUNDING_FEED, fetch_techcrunch_funding_articles
from nous.util.ssrf import BlockedAddressError

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
        location: str | None = None,
    ) -> None:
        self.substring = substring
        self.status = status
        self.body = body
        self.content_type = content_type
        self.raise_network_error = raise_network_error
        # When set, the route returns a redirect to this URL. httpx clients with
        # follow_redirects=True chase the chain to the next matching route.
        self.location = location
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
                if r.location is not None:
                    # Redirect — httpx (follow_redirects=True) chases Location.
                    return httpx.Response(
                        r.status if r.status in (301, 302, 303, 307, 308) else 302,
                        headers={"location": r.location},
                    )
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


def test_matches_funding_keyword_rejects_embedded_substrings() -> None:
    """Keywords must match whole words only — the W-D live false-positive class.

    VentureBeat's only surviving item in a funding-free window was an LLM-evals
    piece kept because "e**valuation**s" substring-matched "valuation". Pin
    every keyword whose substring form appears inside a common English word.
    """
    # The observed live false positive: "evaluations" ⊃ "valuation".
    assert not _matches_funding_keyword("Why LLM evaluations fail in production")
    assert not _matches_funding_keyword("New model evaluation benchmarks released")
    # "praised" / "appraises" ⊃ "raised" / "raises".
    assert not _matches_funding_keyword("Critics praised the new agent framework")
    assert not _matches_funding_keyword("This tool appraises enterprise codebases")
    # "encloses" / "discloses" ⊃ "closes".
    assert not _matches_funding_keyword("The sandbox encloses untrusted code")
    assert not _matches_funding_keyword("Vendor discloses breach details")
    # "misled by" ⊃ "led by".
    assert not _matches_funding_keyword("Users misled by dark patterns")
    # "seedling" ⊃ "seed"; "reseeding" ⊃ "seed".
    assert not _matches_funding_keyword("A seedling program for open source")
    assert not _matches_funding_keyword("Reseeding the cache after deploys")
    # "series a" must not fire inside "series analysis" ("a" is a prefix of
    # "analysis", not a whole word).
    assert not _matches_funding_keyword("A time series analysis toolkit")


def test_matches_funding_keyword_whole_words_and_phrases() -> None:
    """True positives survive the word-boundary tightening."""
    assert _matches_funding_keyword("Acme raises $10M seed round")
    assert _matches_funding_keyword("Acme raised $10M")
    assert _matches_funding_keyword("Acme lands Series A extension")
    assert _matches_funding_keyword("Round led by Sequoia")
    assert _matches_funding_keyword("Acme closes $50M at a $2B valuation")
    # Punctuation adjacent to a keyword is still a word boundary.
    assert _matches_funding_keyword("Funding: the year in enterprise AI")
    assert _matches_funding_keyword("Acme's seed, explained")
    # Multi-word keywords tolerate hyphenation and line wraps.
    assert _matches_funding_keyword("Acme's Series-A round")
    assert _matches_funding_keyword("a round led\nby Sequoia")


def test_funding_keywords_includes_basics() -> None:
    # Sanity guard against accidental list edits dropping core signals.
    for required in ("raised", "funding", "valuation", "series a"):
        assert required in FUNDING_KEYWORDS


# ---------------------------------------------------------------------------
# Relevance guard: article_mentions_company (unit)
# ---------------------------------------------------------------------------
#
# Regression coverage for the live "Aardvark" misattribution: the per-company
# Google News query "<name> funding" returns articles that merely contain a
# generic word. The guard requires the company name to actually appear before
# attributing, biased toward dropping borderline matches.


def test_tokenize_folds_case_and_punctuation() -> None:
    assert _tokenize("Aardvark Therapeutics, Inc.") == [
        "aardvark",
        "therapeutics",
        "inc",
    ]
    assert _tokenize("") == []


def test_phrase_in_tokens_requires_contiguous_subsequence() -> None:
    assert _phrase_in_tokens(["aardvark"], ["aardvark", "raises", "85m"])
    assert _phrase_in_tokens(
        ["aardvark", "therapeutics"],
        _tokenize("Aardvark Therapeutics raises $85M"),
    )
    # Out-of-order / non-contiguous does not count as the phrase.
    assert not _phrase_in_tokens(
        ["aardvark", "therapeutics"], ["therapeutics", "from", "aardvark"]
    )
    # Empty needle never matches.
    assert not _phrase_in_tokens([], ["anything"])


def test_phrase_match_respects_word_boundaries() -> None:
    """Token matching must not fire on a substring inside another word —
    "Ramp" must not match inside "cramped"."""
    assert not _phrase_in_tokens(["ramp"], _tokenize("the schedule felt cramped today"))
    assert _phrase_in_tokens(["ramp"], _tokenize("Ramp raises a Series F"))


class TestArticleMentionsCompany:
    # --- The live Aardvark false positives: all rejected -------------------

    def test_rejects_unrelated_pbs_funding_story(self) -> None:
        assert not article_mentions_company(
            "Aardvark",
            "Donald Trump Cut Funding To PBS, And Now This 'Arthur' TikTok Is Going Viral",
        )

    def test_rejects_unrelated_rugby_fundraiser(self) -> None:
        assert not article_mentions_company(
            "Aardvark",
            "Rugby tournament raises money for local youth charity",
        )

    def test_rejects_unrelated_daycare_funding(self) -> None:
        assert not article_mentions_company(
            "Aardvark",
            "Day-care owners fighting for survival as federal funding runs out",
        )

    # --- Genuine funding articles: kept ------------------------------------

    def test_keeps_genuine_single_token_name_in_title(self) -> None:
        assert article_mentions_company(
            "Aardvark",
            "Aardvark Therapeutics raises $85M Series C to advance obesity drug",
        )

    def test_keeps_genuine_full_phrase_name(self) -> None:
        assert article_mentions_company(
            "Aardvark Therapeutics",
            "Aardvark Therapeutics closes $85M round led by Decheng Capital",
        )

    def test_keeps_when_name_has_corporate_suffix(self) -> None:
        # Suffix is stripped before matching, so "Ramp Inc" still matches a
        # headline that says just "Ramp".
        assert article_mentions_company(
            "Ramp Inc",
            "Ramp raises Series F at $22.5B valuation",
        )

    # --- Risky (short / common-word) names: title is the strong signal -----

    def test_risky_name_rejected_when_only_in_body_without_funding_headline(
        self,
    ) -> None:
        """A common-word name appearing only deep in the body of an article whose
        HEADLINE isn't funding-flavored is NOT attributed — that's exactly the
        incidental-mention false positive we guard against."""
        body = (
            "City council debated the new transit plan for an hour. "
            "A councilor mentioned a ramp near the station. " * 5
        )
        assert not article_mentions_company(
            "Ramp",
            "City council debates new transit plan",
            body=body,
        )

    def test_risky_name_kept_when_body_match_and_funding_headline(self) -> None:
        """A common-word name in the body IS attributed when the headline itself
        is funding-flavored (publisher headline often abbreviates the name)."""
        body = (
            "Ramp, the corporate card startup, announced it has raised a new "
            "round. " + "Details of the financing follow. " * 10
        )
        assert article_mentions_company(
            "Ramp",
            "Fintech startup closes Series F",  # funding-flavored, name not present
            body=body,
        )

    def test_two_token_name_treated_as_risky_needs_title_or_lede(self) -> None:
        # "Acme Robotics" (2 tokens) -> risky. Title without the phrase, and no
        # body, is dropped.
        assert not article_mentions_company(
            "Acme Robotics",
            "A roundup of robotics startups to watch in 2026",
        )
        # But the full phrase in the title keeps it.
        assert article_mentions_company(
            "Acme Robotics",
            "Acme Robotics raises $40M Series B",
        )

    # --- Distinctive (>= 3 token) names: lede match is enough --------------

    def test_distinctive_name_kept_on_body_lede_match(self) -> None:
        """A long, low-collision name is trusted on a body-lede mention even when
        the (truncated) RSS title omits it."""
        body = (
            "Northstar Quantum Systems, a Boston startup, said it raised $50M. "
            + "More detail in the body. " * 10
        )
        assert article_mentions_company(
            "Northstar Quantum Systems",
            "Boston startup lands a fresh round",  # name absent from title
            body=body,
        )

    def test_distinctive_name_rejected_when_absent_everywhere(self) -> None:
        assert not article_mentions_company(
            "Northstar Quantum Systems",
            "Unrelated company raises a big round",
            body="This article is about a completely different firm. " * 20,
        )

    # --- Body relevance is limited to the lede window ----------------------

    def test_body_match_only_counts_within_lede_window(self) -> None:
        """A distinctive name buried PAST the lede window does not rescue an
        article whose title omits it — scanning the whole body would re-admit
        incidental mentions."""
        filler = "Totally unrelated narrative text. " * 40  # >> 600 chars
        body = filler + " Northstar Quantum Systems is mentioned only here."
        assert len(filler) > 600
        assert not article_mentions_company(
            "Northstar Quantum Systems",
            "A story about something else",
            body=body,
        )

    # --- Defensive edge cases ----------------------------------------------

    def test_blank_or_suffix_only_name_fails_closed(self) -> None:
        assert not article_mentions_company("", "Some funding headline")
        # A name that is nothing but a strippable suffix has no anchor token.
        assert not article_mentions_company("Inc", "Some funding headline")


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


async def test_google_news_rss_bypasses_robots_for_feed_surface() -> None:
    """Google News publishes /rss/search as a syndication feed (HTTP 200) while
    its robots.txt ``Disallow: /`` targets the *interactive* crawl surface. The
    RSS endpoint is an exempt feed surface (see ``_ROBOTS_EXEMPT_PREFIXES``): it
    IS fetched despite a disallow-all robots.txt — the throttle + User-Agent
    still apply. Honoring robots literally here returned nothing for every
    per-company query, silently dark-starting funding-news discovery.
    """
    transport = _MockTransport(
        [
            _Route("news.google.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL),
            _Route("news.google.com/rss/search", status=200, body=GOOGLE_NEWS_XML),
        ]
    )

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        results = await client.google_news_rss('"OpenAI" funding', lookback_days=-1)

    assert len(results) > 0, "RSS feed surface must be fetched despite robots disallow-all"
    rss_route = next(r for r in transport._routes if "rss/search" in r.substring)
    assert rss_route.call_count == 1
    # The feed host's robots.txt should not even be consulted for an exempt URL.
    robots_route = next(r for r in transport._routes if "robots.txt" in r.substring)
    assert robots_route.call_count == 0


def test_robots_exempt_matches_only_google_news_rss() -> None:
    """The robots exemption is deliberately narrow — Google News RSS only."""
    assert _is_robots_exempt("https://news.google.com/rss/search?q=x")
    assert _is_robots_exempt("https://news.google.com/rss/topics/abc")
    # Non-feed Google News paths and every other host stay under the robots gate.
    assert not _is_robots_exempt("https://news.google.com/search?q=x")
    assert not _is_robots_exempt("https://news.google.com/")
    assert not _is_robots_exempt("https://techcrunch.com/rss/")
    assert not _is_robots_exempt("https://example.com/article")


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


async def test_fetch_article_body_returns_none_on_blocked_address() -> None:
    """A direct-publisher article URL whose host is SSRF-blocked or unresolvable
    must return None, not propagate BlockedAddressError and crash ingest-news.

    The article-page GET goes through a guarded_async_client, so on a non-public
    or (now that the guard fails closed on DNS failure) unresolvable host it
    raises BlockedAddressError. fetch_article_body must swallow it like the
    other handled fetch errors (robots-block, 4xx, 5xx, network error). robots
    here 404s so the test reaches the page GET that the guard rejects.
    """

    class _BlockingArticleTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            url_str = str(request.url)
            if "robots.txt" in url_str:
                return httpx.Response(404, content=b"Not Found")
            raise BlockedAddressError(f"blocked: {url_str}")

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _BlockingArticleTransport())
        body = await client.fetch_article_body(
            "https://blocked-publisher.example.com/article"
        )

    assert body is None


# ---------------------------------------------------------------------------
# resolve_article — follow the Google News redirect, fetch the real body
# ---------------------------------------------------------------------------

_GN_REDIRECT = "https://news.google.com/rss/articles/CBMiOPAQUE?oc=5"
_REAL_ARTICLE_HTML = (
    "<html><body><main><p>"
    + "Redirect Co raised a $30M Series B led by Acme Ventures. " * 25
    + "</p></main></body></html>"
)


async def test_resolve_article_follows_redirect_and_returns_body() -> None:
    """resolve_article follows the Google News redirect to the real publisher,
    fetches that page, and returns a ResolvedArticle whose url/source point at
    the publisher and whose body is the extracted article text."""
    transport = _MockTransport(
        [
            # The Google News redirect → publisher.
            _Route(
                "news.google.com/rss/articles",
                location="https://realpub.com/redirect-co-series-b",
            ),
            # robots for the publisher allows the fetch.
            _Route("realpub.com/robots.txt", status=404),
            _Route(
                "realpub.com/redirect-co-series-b",
                status=200,
                body=_REAL_ARTICLE_HTML,
            ),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        resolved = await client.resolve_article(_GN_REDIRECT)

    assert resolved is not None
    assert isinstance(resolved, ResolvedArticle)
    assert resolved.url == "https://realpub.com/redirect-co-series-b"
    assert resolved.source == "realpub.com"
    assert len(resolved.body) >= MIN_BODY_CHARS
    assert "Series B led by Acme Ventures" in resolved.body


async def test_resolve_article_returns_none_when_publisher_robots_blocks() -> None:
    """robots.txt is checked on the RESOLVED publisher URL (the Google News
    robots-exemption does NOT extend to the destination). A disallow → None."""
    transport = _MockTransport(
        [
            _Route(
                "news.google.com/rss/articles",
                location="https://blocked-pub.com/article",
            ),
            _Route(
                "blocked-pub.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL
            ),
            _Route(
                "blocked-pub.com/article", status=200, body=_REAL_ARTICLE_HTML
            ),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        resolved = await client.resolve_article(_GN_REDIRECT)

    assert resolved is None


async def test_resolve_article_returns_none_on_thin_body() -> None:
    """A resolved page below MIN_BODY_CHARS (paywall stub / consent shell) → None."""
    short_html = "<html><body><p>Subscribe to read.</p></body></html>"
    transport = _MockTransport(
        [
            _Route(
                "news.google.com/rss/articles",
                location="https://thinpub.com/article",
            ),
            _Route("thinpub.com/robots.txt", status=404),
            _Route("thinpub.com/article", status=200, body=short_html),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        resolved = await client.resolve_article(_GN_REDIRECT)

    assert resolved is None


async def test_resolve_article_returns_none_when_no_redirect() -> None:
    """If the Google News URL does not redirect to a publisher (it 200s with the
    consent interstitial on news.google.com itself), there's no real article to
    resolve — return None so the caller falls back to the headline."""
    transport = _MockTransport(
        [
            _Route("news.google.com/robots.txt", status=404),
            _Route(
                "news.google.com/rss/articles",
                status=200,
                body="<html><body><p>consent interstitial</p></body></html>",
            ),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        resolved = await client.resolve_article(_GN_REDIRECT)

    assert resolved is None


async def test_resolve_article_returns_none_on_network_error() -> None:
    """A network failure following the redirect → None (caller falls back)."""
    transport = _MockTransport(
        [
            _Route(
                "news.google.com/rss/articles",
                location="https://badpub.com/article",
            ),
            _Route("badpub.com/robots.txt", status=404),
            _Route("badpub.com/article", raise_network_error=True),
        ]
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, transport)
        resolved = await client.resolve_article(_GN_REDIRECT)

    assert resolved is None


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
