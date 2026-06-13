"""Lightspeed (lsvp.com) portfolio adapter.

https://lsvp.com/portfolio/ is server-rendered: each company is an
``<li data-company-id="...">`` inside ``ul.companies-list``. The list cards
expose name + founders + invest-stage but NOT the company's homepage URL —
those live on the per-company detail subpages. We surface name only and let
M2's resolve-homepages stage fill website in later.

Dual-fund and India-only cards nest a ``span.info-icon-wrapper`` fund-badge
("LSVP and LSIP Investment" / "LSIP Investment") in the h5 whose text bleeds
into the name under deep-text extraction; US-only cards' h5s are pure text.
We use ``text(deep=False, strip=True)`` to read only the direct text node of
the h5, bypassing the badge span entirely.
Cards with ``data-investor="lsip"`` mark Lightspeed India Partners-only
holdings; this US-only catalog skips them.
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
            # data-investor marks which fund(s) hold the company:
            # 'lsvp' (US), 'lsip' (Lightspeed India Partners), 'both'.
            # India-only holdings are out of scope (US-only catalog).
            if item.attributes.get("data-investor") == "lsip":
                continue
            heading = item.css_first(".detail h5")
            if heading is None:
                continue
            # deep=False: the h5 nests a span.info-icon-wrapper fund-badge
            # ("LSVP and LSIP Investment") that deep text concatenates into
            # the name — the source of 96 mangled prod rows.
            name = heading.text(deep=False, strip=True)
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
