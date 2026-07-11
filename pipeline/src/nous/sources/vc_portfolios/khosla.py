"""Khosla Ventures portfolio adapter.

https://www.khoslaventures.com/portfolio/ is a Webflow landing page that only
embeds a small "spotlight" subset (~15 companies) directly. The full portfolio
is split across nine category subpages linked from the landing page —
``/category/enterprise``, ``/category/fintech``, etc. We scrape the landing
page to discover category URLs, then aggregate company cards across all of
them.

On each page, companies are ``<a class="company-slide w-inline-block">``
nodes carrying ``href`` (homepage URL), child ``<img alt="<Name>">``, and a
``<div class="text-block-17">`` tagline.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient, RobotsBlockedError
from nous.sources.vc_portfolios.base import PortfolioEntry, ensure_entries

logger = logging.getLogger(__name__)


class KhoslaAdapter:
    firm = "khosla"
    PORTFOLIO_URL = "https://www.khoslaventures.com/portfolio/"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        landing_html = (await client.fetch(self.PORTFOLIO_URL)).content
        landing_tree = HTMLParser(landing_html)

        seen: dict[str, PortfolioEntry] = {}
        # Seed with whatever the landing page shows directly.
        for entry in _parse_cards(landing_tree, self.firm, self.PORTFOLIO_URL):
            seen.setdefault(entry.name, entry)

        category_urls = _discover_category_urls(landing_tree, self.PORTFOLIO_URL)
        for cat_url in category_urls:
            try:
                cat_html = (await client.fetch(cat_url)).content
            except (RobotsBlockedError, httpx.HTTPStatusError, httpx.RequestError) as exc:
                # Individual category page failures shouldn't sink the whole adapter.
                logger.warning("khosla: skipping category %s: %s", cat_url, exc)
                continue
            for entry in _parse_cards(HTMLParser(cat_html), self.firm, self.PORTFOLIO_URL):
                seen.setdefault(entry.name, entry)

        # Zero *total* across landing + all categories is a structural miss;
        # an individual empty/failed category page above is tolerated.
        return ensure_entries(
            list(seen.values()),
            self.firm,
            context="no a.company-slide cards on landing or category pages",
        )


def _parse_cards(tree: HTMLParser, firm: str, source_url: str) -> list[PortfolioEntry]:
    entries: list[PortfolioEntry] = []
    for card in tree.css("a.company-slide"):
        img = card.css_first("img")
        if img is None:
            continue
        name = (img.attributes.get("alt") or "").strip()
        if not name:
            continue
        href = card.attributes.get("href")
        website = href.strip() if isinstance(href, str) and href.strip() else None
        desc_node = card.css_first(".text-block-17")
        description = desc_node.text(strip=True) if desc_node else None
        if description == "":
            description = None
        entries.append(
            PortfolioEntry(
                firm=firm,
                name=name,
                website=website,
                description=description,
                source_url=source_url,
            )
        )
    return entries


def _discover_category_urls(landing_tree: HTMLParser, base_url: str) -> list[str]:
    """Find /category/<slug> links on the landing page, deduped + ordered."""
    base_host = urlparse(base_url).netloc
    seen: set[str] = set()
    ordered: list[str] = []
    for anchor in landing_tree.css("a[href*='/category/']"):
        href = anchor.attributes.get("href")
        if not isinstance(href, str):
            continue
        absolute = urljoin(base_url, href.strip())
        parsed = urlparse(absolute)
        if parsed.netloc != base_host:
            continue
        if "/category/" not in parsed.path:
            continue
        # Normalise: strip query/fragment.
        normalised = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if normalised in seen:
            continue
        seen.add(normalised)
        ordered.append(normalised)
    return ordered
