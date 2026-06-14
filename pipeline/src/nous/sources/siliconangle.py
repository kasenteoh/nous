"""SiliconANGLE RSS adapter.

SiliconANGLE (https://siliconangle.com) covers US enterprise-tech broadly —
funding announcements sit alongside product news, events, and opinion.
The main feed (``/feed/``) is a general-purpose RSS stream, so we apply
``FUNDING_KEYWORDS`` filtering (same as ``google_news_rss``) to keep only
articles that mention a funding signal in the title or snippet.

No more-specific SiliconANGLE funding category feed was found at time of
writing: the site's WordPress category taxonomy does not expose a clean
``/category/funding/feed/`` or equivalent that reliably covers only startup
funding stories. The main feed with keyword filtering is the right approach.

Body extraction is delegated to ``NewsClient.fetch_article_body`` so the
robots-check, throttle, and noise-stripping logic stay in one place.
"""

from __future__ import annotations

import logging

import httpx

from nous.sources.news import NewsArticleResult, NewsClient, RobotsBlockedError

logger = logging.getLogger(__name__)

SILICONANGLE_FEED = "https://siliconangle.com/feed/"


async def fetch_siliconangle_funding_articles(
    client: NewsClient,
    *,
    lookback_days: int = 14,
) -> list[NewsArticleResult]:
    """Pull the SiliconANGLE RSS feed, filter to funding stories, return entries.

    The main feed is broad enterprise-tech news, so funding-keyword filtering
    (``require_keywords=True``) is essential — without it the results would be
    dominated by product coverage, event recaps, and opinion pieces.

    Returns an empty list on robots block, HTTP error, or network failure —
    the caller treats this like any other empty-source case rather than
    erroring out the whole pipeline run.
    """
    try:
        xml_text = await client.fetch_text(SILICONANGLE_FEED)
    except RobotsBlockedError:
        logger.warning("SiliconANGLE feed blocked by robots.txt")
        return []
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning("SiliconANGLE feed fetch failed: %s", exc)
        return []

    # The main feed is general tech news — apply keyword filter so only
    # funding-signal articles reach the pipeline.
    return client._parse_rss(
        xml_text,
        lookback_days=lookback_days,
        require_keywords=True,
    )
