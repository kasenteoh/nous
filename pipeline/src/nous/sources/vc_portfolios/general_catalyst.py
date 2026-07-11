"""General Catalyst portfolio adapter.

https://www.generalcatalyst.com/portfolio is a Webflow site. Each portfolio
company is a card linking to ``/companies/<slug>``. In the main (alphabetical)
list the company name lives in the card's ``<h2>`` while the anchor text is a
generic "Learn More"; in the featured row the anchor text is the name itself.
We read the name from the card heading first, fall back to the anchor text
(ignoring generic CTAs), then to the slug. The link is GC's own detail page,
not the company homepage, so ``website`` is ``None`` and resolve-homepages
fills it in later. We dedup by name (featured companies also appear in the list).
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.base import PortfolioEntry, ensure_entries

_GENERIC_LINK_TEXT = {"", "learn more", "view", "read more", "visit"}


class GeneralCatalystAdapter:
    firm = "general_catalyst"
    PORTFOLIO_URL = "https://www.generalcatalyst.com/portfolio"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        tree = HTMLParser(html)
        entries: list[PortfolioEntry] = []
        seen: set[str] = set()
        for anchor in tree.css('a[href*="/companies/"]'):
            href = (anchor.attributes.get("href") or "").strip()
            slug = href.partition("/companies/")[2].split("?")[0].split("#")[0].strip("/")
            # Only leaf company pages (/companies/<slug>), not nav or nested paths.
            if not slug or "/" in slug:
                continue

            name = ""
            parent = anchor.parent
            if parent is not None:
                heading = parent.css_first("h2")
                if heading is not None:
                    name = heading.text(strip=True)
            if not name:
                text = anchor.text(strip=True)
                if text.lower() not in _GENERIC_LINK_TEXT:
                    name = text
            if not name:
                name = _slug_to_name(slug)

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
            entries, self.firm, context='no a[href*="/companies/"] anchors matched'
        )


def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").title()
