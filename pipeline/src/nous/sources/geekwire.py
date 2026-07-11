"""GeekWire funding-tag RSS adapter.

GeekWire (https://www.geekwire.com) is the Pacific-Northwest tech desk —
strong, early coverage of Seattle-area startups that national outlets pick up
late or never. Investigated at adapter-writing time (2026-07): the site's
``/fundings/`` page is an HTML tracker (no feed behind it), but the editorial
**funding tag** publishes a clean WordPress feed at ``/tag/funding/feed/``
where every item is a funding story ("KredosAI raises $7M…", "Seattle biotech
heavy-hitters emerge from stealth with $46M…").

Because the tag IS the funding filter, we mirror the TechCrunch venture-tag
adapter and pass ``require_keywords=False`` — several genuine items
("…emerge from stealth with $46M") carry no ``FUNDING_KEYWORDS`` hit in the
title, and the keyword gate would drop them for no gain in precision. The
downstream LLM headline-extraction step already rejects non-announcement
items (fund raises, roundups) by returning ``is_funding_announcement=false``.

Note: GeekWire sits behind Cloudflare and 403s TLS fingerprints it dislikes
(curl), but serves our httpx client with the identifying User-Agent fine.
The snippet is truncated to ``SNIPPET_MAX_CHARS`` for symmetry with the
VentureBeat adapter (GeekWire excerpts are modest, but the cap keeps the LLM
prompt size bounded by construction).
"""

from __future__ import annotations

import logging

import httpx

from nous.sources.news import NewsArticleResult, NewsClient, RobotsBlockedError

logger = logging.getLogger(__name__)

GEEKWIRE_FUNDING_FEED = "https://www.geekwire.com/tag/funding/feed/"

# Same bound as sources.venturebeat.SNIPPET_MAX_CHARS — see that module's
# docstring for the rationale.
SNIPPET_MAX_CHARS = 600


async def fetch_geekwire_funding_articles(
    client: NewsClient,
    *,
    lookback_days: int = 14,
) -> list[NewsArticleResult]:
    """Pull GeekWire's funding-tag RSS, filter by ``lookback_days``, return entries.

    ``lookback_days=14`` (vs. TC's 7): the funding tag is applied editorially
    and updates a few times a week, so a wider window guards against slow news
    weeks leaving the whole feed outside the cutoff.

    Returns an empty list on robots block, HTTP error, or network failure —
    the caller treats this like any other empty-source case rather than
    erroring out the whole pipeline run.
    """
    try:
        xml_text = await client.fetch_text(GEEKWIRE_FUNDING_FEED)
    except RobotsBlockedError:
        logger.warning("GeekWire funding feed blocked by robots.txt")
        return []
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning("GeekWire funding feed fetch failed: %s", exc)
        return []

    # The funding tag IS the filter — every item is a funding story, and some
    # genuine ones carry no FUNDING_KEYWORDS hit (see module docstring).
    results = client._parse_rss(
        xml_text,
        lookback_days=lookback_days,
        require_keywords=False,
    )
    return [
        r.model_copy(update={"raw_content": r.raw_content[:SNIPPET_MAX_CHARS]})
        if len(r.raw_content) > SNIPPET_MAX_CHARS
        else r
        for r in results
    ]
