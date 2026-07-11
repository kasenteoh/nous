"""a16z portfolio adapter.

The portfolio page at https://a16z.com/portfolio/ ships every company as a
JSON literal assigned to ``window.a16z_portfolio_companies`` in a ``<script>``
tag — ~875 entries with ``title``/``web``/``year_founded`` fields. We don't
walk CSS; we regex the literal out of the HTML and ``json.loads`` it.
"""

from __future__ import annotations

import json
import logging
import re

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios._json_island import find_balanced
from nous.sources.vc_portfolios.base import PortfolioEntry, is_placeholder_name

logger = logging.getLogger(__name__)


class A16zAdapter:
    firm = "a16z"
    PORTFOLIO_URL = "https://a16z.com/portfolio/"

    _ISLAND_RE = re.compile(
        r"a16z_portfolio_companies\s*=\s*(\[)", re.DOTALL
    )

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        array_text = find_balanced(html, self._ISLAND_RE)
        if array_text is None:
            raise RuntimeError(
                "a16z: window.a16z_portfolio_companies array not found in page; "
                "DOM likely changed."
            )
        companies = json.loads(array_text)
        entries: list[PortfolioEntry] = []
        for company in companies:
            if not isinstance(company, dict):
                continue
            name = company.get("title")
            if not isinstance(name, str) or is_placeholder_name(name):
                # Skip entries with missing, empty, or bracketed placeholder names
                # (e.g. "[untitled]" for stealth-mode companies). The real company
                # is untitled.stream; its a16z JSON entry carries "[untitled]" as
                # the literal title value because its legal name is intentionally
                # withheld. Storing that verbatim corrupts the catalog.
                if isinstance(name, str) and name.strip():
                    logger.debug(
                        "a16z: skipping entry with placeholder name %r (web=%s)",
                        name,
                        company.get("web"),
                    )
                continue
            web = company.get("web")
            website: str | None = web if isinstance(web, str) and web.strip() else None
            entries.append(
                PortfolioEntry(
                    firm=self.firm,
                    name=name.strip(),
                    website=website,
                    description=None,
                    source_url=self.PORTFOLIO_URL,
                )
            )
        return entries
