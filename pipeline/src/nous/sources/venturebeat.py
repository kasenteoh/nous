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
  KB), not an excerpt. That breaks naive keyword filtering: a multi-KB
  enterprise-research piece almost always mentions "funding"/"raised"
  *somewhere*, so matching over the full body admits nearly every item
  (verified live 2026-07: 6 of 7 items in a window with zero funding stories
  passed). Each false positive burns an LLM headline-extraction call every
  ingest run while it sits in the feed window. We therefore match
  ``FUNDING_KEYWORDS`` against the **title + the first ``SNIPPET_MAX_CHARS``
  of the body** — a genuine funding story declares itself in the lede
  ("raised $100 million in a Series B funding round" appears in Railway's
  first paragraph) — and store that same bounded lede as ``raw_content`` so
  the DeepSeek prompt stays cheap by construction.
"""

from __future__ import annotations

import logging

import httpx

from nous.sources.news import (
    NewsArticleResult,
    NewsClient,
    RobotsBlockedError,
    _matches_funding_keyword,
)

logger = logging.getLogger(__name__)

VENTUREBEAT_FEED = "https://venturebeat.com/feed"

# Lede window: keywords are matched against (and raw_content truncated to)
# this many chars of the full-body description. Mirrors the spirit of
# ``news.RELEVANCE_BODY_PORTION_CHARS`` (600) — enough lede to declare a
# funding round and name the company, bounded enough to keep the LLM prompt
# cheap and the filter precise (see module docstring).
SNIPPET_MAX_CHARS = 600


async def fetch_venturebeat_funding_articles(
    client: NewsClient,
    *,
    lookback_days: int = 7,
) -> list[NewsArticleResult]:
    """Pull the VentureBeat main RSS feed, filter to funding stories.

    The main feed is broad enterprise-tech/AI coverage, so funding filtering
    is essential — but it must run over the title + lede only, NOT the
    full-body description ``_parse_rss`` would use with
    ``require_keywords=True`` (see module docstring). We parse unfiltered,
    then apply ``FUNDING_KEYWORDS`` to the bounded lede ourselves.

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
        require_keywords=False,
    )
    kept: list[NewsArticleResult] = []
    for result in results:
        lede = result.raw_content[:SNIPPET_MAX_CHARS]
        # Precision gate: the keyword must appear in the headline or lede —
        # an incidental mention deep in a full-body description is exactly
        # the false-positive mode this adapter must not ship to the LLM.
        if not _matches_funding_keyword(f"{result.title}\n{lede}"):
            continue
        if len(result.raw_content) > SNIPPET_MAX_CHARS:
            result = result.model_copy(update={"raw_content": lede})
        kept.append(result)
    return kept
