"""PR Newswire venture-capital RSS adapter.

The PR Newswire venture-capital category feed (`/rss/financial-services-latest-news/
venture-capital-list.rss`) is a high-volume, editorially-curated press-release
surface specifically for VC and startup-funding news.  Because it is already
filtered to the Venture Capital subject category, virtually every entry is a
funding-signal story.  We still apply FUNDING_KEYWORDS to exclude any strays
(e.g. personnel announcements that are mis-tagged) and keep the same discipline
as the Google News adapter.

robots.txt for www.prnewswire.com does not disallow the /rss/ path, so the
standard robots gate applies and is expected to allow the fetch.
"""

from __future__ import annotations

import logging

import httpx

from nous.sources.news import NewsArticleResult, NewsClient, RobotsBlockedError

logger = logging.getLogger(__name__)

PRNEWSWIRE_VC_FEED = (
    "https://www.prnewswire.com/rss/financial-services-latest-news/venture-capital-list.rss"
)


async def fetch_prnewswire_funding_articles(
    client: NewsClient,
    *,
    lookback_days: int = 14,
) -> list[NewsArticleResult]:
    """Pull the PR Newswire VC RSS, filter by ``lookback_days`` and funding keywords.

    Returns an empty list on robots block, HTTP error, or network failure —
    the caller treats this like any other empty-source case rather than
    erroring out the whole pipeline run.

    ``lookback_days=14`` is the default (vs. TC's 7) because PR Newswire press
    releases self-publish rather than being picked up by an editorial desk, so
    embargoed releases often land a few days after the event — a wider window
    reduces missed coverage.
    """
    try:
        xml_text = await client.fetch_text(PRNEWSWIRE_VC_FEED)
    except RobotsBlockedError:
        logger.warning("PR Newswire VC feed blocked by robots.txt")
        return []
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning("PR Newswire VC feed fetch failed: %s", exc)
        return []

    # Apply funding-keyword filter: while the feed IS the VC subject category,
    # it occasionally includes non-funding VC news (fund expansions, personnel
    # announcements) whose headlines don't signal a specific investment event.
    # Filtering keeps only items that the pipeline's extraction stage can
    # meaningfully act on.
    return client._parse_rss(
        xml_text,
        lookback_days=lookback_days,
        require_keywords=True,
    )
