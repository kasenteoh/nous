"""Crunchbase News RSS adapter.

Crunchbase News (news.crunchbase.com) covers private-market activity —
funding rounds, fund closes, unicorn tracking, and sector snapshots — but
also publishes layoffs trackers, IPO commentary, and editorial pieces that
contain no funding signal. We therefore apply the standard FUNDING_KEYWORDS
filter (same as google_news_rss) rather than treating the feed as a pure
funding-news surface.

The feed URL (``CB_NEWS_FEED``) is Crunchbase News's own published RSS
endpoint. It returns HTTP 200 with valid XML to identified crawlers and is
designed for programmatic consumption.
"""

from __future__ import annotations

import logging

import httpx

from nous.sources.news import FUNDING_KEYWORDS, NewsArticleResult, NewsClient, RobotsBlockedError

logger = logging.getLogger(__name__)

CB_NEWS_FEED = "https://news.crunchbase.com/feed/"

# Crunchbase News is not a funding-exclusive surface: it also publishes
# layoffs trackers, IPO analyses, and editorial commentary. We apply
# FUNDING_KEYWORDS to keep only articles with an explicit funding signal,
# consistent with the pipeline's spec §5.5 discipline.
_REQUIRE_KEYWORDS: bool = True

# Expose the keyword tuple so tests can assert on the contract without
# hard-coding individual strings.
__all__ = [
    "CB_NEWS_FEED",
    "FUNDING_KEYWORDS",
    "fetch_crunchbase_news_funding_articles",
]


async def fetch_crunchbase_news_funding_articles(
    client: NewsClient,
    *,
    lookback_days: int = 14,
) -> list[NewsArticleResult]:
    """Pull the Crunchbase News RSS feed, apply lookback + keyword filter.

    Crunchbase News publishes a mix of funding round coverage and editorial
    content. Only items whose title + snippet contain at least one
    ``FUNDING_KEYWORDS`` hit are returned, so that the output matches the
    pipeline's expectations for a funding-news source.

    Returns an empty list on robots block, HTTP error, or network failure —
    the caller treats this like any other empty-source case rather than
    erroring out the whole pipeline run.

    Args:
        client: An open ``NewsClient`` context-managed instance. Robots
            checking, per-domain throttling, User-Agent identification, and
            SSRF protection are all handled inside ``client``.
        lookback_days: Drop articles published more than this many days ago.
            Pass ``-1`` to disable the cutoff (useful for tests against a
            frozen fixture).
    """
    try:
        xml_text = await client.fetch_text(CB_NEWS_FEED)
    except RobotsBlockedError:
        logger.warning("Crunchbase News feed blocked by robots.txt")
        return []
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning("Crunchbase News feed fetch failed: %s", exc)
        return []

    return client._parse_rss(
        xml_text,
        lookback_days=lookback_days,
        require_keywords=_REQUIRE_KEYWORDS,
    )
