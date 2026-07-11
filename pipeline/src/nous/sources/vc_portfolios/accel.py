"""Accel portfolio adapter.

https://www.accel.com/companies is a Next.js (App Router / RSC) page, but the
server-rendered HTML already contains one anchor per company:
``<a href="/companies/<slug>" aria-label="View <Name> company details">``. We
read the name out of the ``aria-label`` rather than walking the RSC flight
payload. The ``href`` points at Accel's own detail page, not the company
homepage, so ``website`` is ``None`` and resolve-homepages fills it in later.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.base import PortfolioEntry, ensure_entries

_VIEW_ARIA_RE = re.compile(r"^View\s+(.+?)\s+company details$", re.IGNORECASE)


class AccelAdapter:
    firm = "accel"
    PORTFOLIO_URL = "https://www.accel.com/companies"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        tree = HTMLParser(html)
        entries: list[PortfolioEntry] = []
        seen: set[str] = set()
        for anchor in tree.css("a[aria-label]"):
            label = anchor.attributes.get("aria-label") or ""
            match = _VIEW_ARIA_RE.match(label.strip())
            if match is None:
                continue
            name = match.group(1).strip()
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
            entries, self.firm, context="no 'View <Name> company details' aria-labels matched"
        )
