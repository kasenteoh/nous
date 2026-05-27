"""TechCrunch venture-tag RSS adapter.

The TC venture category feed is itself a funding-news filter — every entry
is a VC/startup-funding story — so we don't apply the FUNDING_KEYWORDS
match the way ``google_news_rss`` does. We just pull the feed, apply the
lookback window, and return the deduplicated entries.

Body extraction is delegated to ``NewsClient.fetch_article_body`` so the
robots-check, throttle, and noise-stripping logic stay in one place.
"""

from __future__ import annotations

import logging

import httpx

from nous.sources.news import NewsArticleResult, NewsClient, RobotsBlockedError

logger = logging.getLogger(__name__)

TC_FUNDING_FEED = "https://techcrunch.com/category/venture/feed/"


async def fetch_techcrunch_funding_articles(
    client: NewsClient,
    *,
    lookback_days: int = 7,
) -> list[NewsArticleResult]:
    """Pull the TC venture-tag RSS, filter by ``lookback_days``, return entries.

    Returns an empty list on robots block, HTTP error, or network failure —
    the caller treats this like any other empty-source case rather than
    erroring out the whole pipeline run.
    """
    try:
        xml_text = await client._fetch_text(TC_FUNDING_FEED)
    except RobotsBlockedError:
        logger.warning("TechCrunch venture feed blocked by robots.txt")
        return []
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning("TechCrunch venture feed fetch failed: %s", exc)
        return []

    # TC venture tag IS the keyword filter — every post is a funding story.
    return client._parse_rss(
        xml_text,
        lookback_days=lookback_days,
        require_keywords=False,
    )
