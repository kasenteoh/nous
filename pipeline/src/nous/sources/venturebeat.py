"""VentureBeat RSS adapter.

VentureBeat (https://venturebeat.com) covers enterprise tech and AI broadly;
funding announcements ("Railway secures $100M…", "Listen Labs raises $69M…")
sit alongside a heavy volume of research/editorial pieces. Investigated at
adapter-writing time (2026-07): there is **no funding-specific feed** —
``/category/venture/feed`` and ``/tag/funding/feed`` 404, and the category
feeds (``/category/ai/feed``, ``/category/business/feed``) are the same
7-item window sliced by topic. The main feed is the broadest single surface,
so we use it with ``FUNDING_KEYWORDS`` filtering (same as SiliconANGLE).

The feed window is only ~7 items, but ingest-news runs every 3 hours and
VentureBeat publishes well under 7 posts per 3-hour window, so nothing is
missed in steady state.

Quirks:

- ``https://venturebeat.com/feed/`` 308-redirects to ``/feed`` (no trailing
  slash); we fetch the canonical URL directly to save the hop.
- VentureBeat's RSS ``<description>`` carries the FULL article body (several
  KB), not an excerpt. ``raw_content`` feeds the headline-company LLM prompt
  in ingest-news, so we truncate it to ``SNIPPET_MAX_CHARS`` — the funded
  company is always named in the lede, and unbounded snippets would
  materially grow DeepSeek input volume for no extraction benefit. The
  keyword filter inside ``_parse_rss`` runs on the full text *before* the
  truncation, so recall is unaffected.
"""

from __future__ import annotations

import logging

import httpx

from nous.sources.news import NewsArticleResult, NewsClient, RobotsBlockedError

logger = logging.getLogger(__name__)

VENTUREBEAT_FEED = "https://venturebeat.com/feed"

# Cap the stored RSS snippet: VB descriptions are full article bodies, and the
# funded company is named in the lede. Mirrors the spirit of
# ``news.RELEVANCE_BODY_PORTION_CHARS`` (600) — enough lede to identify the
# company, bounded enough to keep the LLM prompt cheap.
SNIPPET_MAX_CHARS = 600


async def fetch_venturebeat_funding_articles(
    client: NewsClient,
    *,
    lookback_days: int = 7,
) -> list[NewsArticleResult]:
    """Pull the VentureBeat main RSS feed, filter to funding stories.

    The main feed is broad enterprise-tech/AI coverage, so funding-keyword
    filtering (``require_keywords=True``) is essential — without it the
    results would be dominated by research pieces and product coverage.

    Returns an empty list on robots block, HTTP error, or network failure —
    the caller treats this like any other empty-source case rather than
    erroring out the whole pipeline run.
    """
    try:
        xml_text = await client.fetch_text(VENTUREBEAT_FEED)
    except RobotsBlockedError:
        logger.warning("VentureBeat feed blocked by robots.txt")
        return []
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning("VentureBeat feed fetch failed: %s", exc)
        return []

    results = client._parse_rss(
        xml_text,
        lookback_days=lookback_days,
        require_keywords=True,
    )
    # Truncate AFTER filtering: the keyword match sees the full description,
    # the stored snippet stays bounded (see module docstring).
    return [
        r.model_copy(update={"raw_content": r.raw_content[:SNIPPET_MAX_CHARS]})
        if len(r.raw_content) > SNIPPET_MAX_CHARS
        else r
        for r in results
    ]
