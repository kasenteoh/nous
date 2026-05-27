"""Lightspeed (lsvp.com) portfolio adapter.

https://lsvp.com/portfolio/ is server-rendered: each company is an
``<li data-company-id="...">`` inside ``ul.companies-list``. The list cards
expose name + founders + invest-stage but NOT the company's homepage URL —
those live on the per-company detail subpages. We surface name only and let
M2's resolve-homepages stage fill website in later.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.base import PortfolioEntry


class LightspeedAdapter:
    firm = "lightspeed"
    PORTFOLIO_URL = "https://lsvp.com/portfolio/"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        tree = HTMLParser(html)
        entries: list[PortfolioEntry] = []
        seen: set[str] = set()
        for item in tree.css("ul.companies-list li[data-company-id]"):
            heading = item.css_first(".detail h5")
            if heading is None:
                continue
            name = heading.text(strip=True)
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
