"""Bessemer Venture Partners portfolio adapter.

https://www.bvp.com/companies is server-rendered (WordPress). Each company is a
``<div class="company">`` whose ``<h3 class="name">`` (wrapping an
``<a class="name">``) carries the company name. The link points at BVP's own
detail page — not the company homepage — so ``website`` is ``None`` and the
resolve-homepages stage fills it in later. The page renders each company twice
(a grid view and a list view), so we dedup by name.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.base import PortfolioEntry, ensure_entries


class BessemerAdapter:
    firm = "bessemer"
    PORTFOLIO_URL = "https://www.bvp.com/companies"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        tree = HTMLParser(html)
        entries: list[PortfolioEntry] = []
        seen: set[str] = set()
        for card in tree.css("div.company"):
            name_node = card.css_first("h3") or card.css_first(".name")
            if name_node is None:
                continue
            name = name_node.text(strip=True)
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
        return ensure_entries(
            entries, self.firm, context="no div.company cards matched"
        )
