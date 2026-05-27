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
from nous.sources.vc_portfolios.base import PortfolioEntry

logger = logging.getLogger(__name__)


class A16zAdapter:
    firm = "a16z"
    PORTFOLIO_URL = "https://a16z.com/portfolio/"

    _ISLAND_RE = re.compile(
        r"a16z_portfolio_companies\s*=\s*(\[)", re.DOTALL
    )

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        array_text = _extract_balanced_array(html, self._ISLAND_RE)
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
            if not isinstance(name, str) or not name.strip():
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


def _extract_balanced_array(html: str, start_re: re.Pattern[str]) -> str | None:
    """Return the balanced ``[...]`` literal starting where ``start_re`` matches.

    Walks brackets respecting string literals so commas/brackets inside quoted
    strings don't fool the counter.
    """
    match = start_re.search(html)
    if not match:
        return None
    start = match.end() - 1  # at '['
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(html)):
        ch = html[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if in_string:
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    return None
