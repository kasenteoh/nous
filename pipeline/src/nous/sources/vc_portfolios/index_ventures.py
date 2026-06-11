"""Index Ventures portfolio adapter.

https://www.indexventures.com/companies is server-rendered: each company is a
``<li class="companies__relationships__list__item js-company">`` whose inner
``<a class="...__link">`` holds the name, optionally followed by a
``<span class="ticker-symbol">`` (e.g. "NASDAQ: DIBS") that we strip off. The
list link points at Index's own detail subpage, not the company homepage, so
``website`` is ``None`` and resolve-homepages fills it in later.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.base import PortfolioEntry


class IndexVenturesAdapter:
    firm = "index_ventures"
    PORTFOLIO_URL = "https://www.indexventures.com/companies"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        tree = HTMLParser(html)
        entries: list[PortfolioEntry] = []
        seen: set[str] = set()
        for item in tree.css("li.js-company"):
            link = item.css_first("a")
            if link is None:
                continue
            # The anchor text is "<Name> <ticker>"; drop the ticker span so the
            # name doesn't carry "NASDAQ: DIBS" etc.
            ticker = link.css_first(".ticker-symbol")
            if ticker is not None:
                ticker.decompose()
            name = link.text(strip=True)
            if not name or name in seen:
                continue
            seen.add(name)
            entries.append(
                PortfolioEntry(
                    firm=self.firm,
                    name=name,
                    website=None,
                    description=None,
                    source_url=self.PORTFOLIO_URL,
                )
            )
        return entries
