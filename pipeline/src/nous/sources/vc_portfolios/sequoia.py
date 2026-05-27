"""Sequoia Capital portfolio adapter.

https://www.sequoiacap.com/companies/ is server-rendered as a FacetWP-driven
table. Each company is a ``<tr>`` whose ``<th class="company-listing__head">``
holds the name and whose ``<td class="company-listing__text">`` holds the
one-line description. The page does not expose a website URL per row — only
the Sequoia detail-page link — so ``website`` is ``None`` for every entry and
the M2 resolve-homepages stage fills it in later.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.base import PortfolioEntry


class SequoiaAdapter:
    firm = "sequoia"
    PORTFOLIO_URL = "https://www.sequoiacap.com/companies/"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        tree = HTMLParser(html)
        entries: list[PortfolioEntry] = []
        for row in tree.css("tbody.facetwp-template tr"):
            head = row.css_first("th.company-listing__head")
            if head is None:
                continue
            name = head.text(strip=True)
            if not name:
                continue
            desc_node = row.css_first("td.company-listing__text")
            description = desc_node.text(strip=True) if desc_node else None
            if description == "":
                description = None
            entries.append(
                PortfolioEntry(
                    firm=self.firm,
                    name=name,
                    website=None,
                    description=description,
                    source_url=self.PORTFOLIO_URL,
                )
            )
        return entries
