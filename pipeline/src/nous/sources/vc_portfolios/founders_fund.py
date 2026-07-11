"""Founders Fund portfolio adapter.

https://foundersfund.com/portfolio/ embeds the company list as a JSON object
assigned to ``window.__data`` in a ``<script>`` tag. Each entry has
``title.rendered`` (display name), ``content.rendered`` (HTML description),
and ``profiles`` (HTML blob whose first ``<a>Website</a>`` is the homepage).

The page bundles ~62 companies — well above the M3 ≥50 threshold. We strip
HTML out of the description and validate the website URL has a non-empty host
(FF occasionally emits malformed `http:///example.com` triple-slash URLs that
we surface as ``None`` rather than poisoning the DB).
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios._json_island import find_balanced
from nous.sources.vc_portfolios.base import PortfolioEntry

logger = logging.getLogger(__name__)


class FoundersFundAdapter:
    firm = "founders_fund"
    PORTFOLIO_URL = "https://foundersfund.com/portfolio/"

    _ISLAND_RE = re.compile(r"window\.__data\s*=\s*(\{)", re.DOTALL)
    _WEBSITE_RE = re.compile(
        r'href=["\']([^"\']+)["\'][^>]*>\s*Website', re.IGNORECASE
    )

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        blob = find_balanced(html, self._ISLAND_RE)
        if blob is None:
            raise RuntimeError(
                "founders_fund: window.__data object not found; DOM likely changed."
            )
        data = json.loads(blob)
        companies = data.get("companies") if isinstance(data, dict) else None
        if not isinstance(companies, list):
            raise RuntimeError("founders_fund: window.__data.companies missing or wrong type")

        entries: list[PortfolioEntry] = []
        for company in companies:
            if not isinstance(company, dict):
                continue
            title = (company.get("title") or {}).get("rendered")
            if not isinstance(title, str) or not title.strip():
                continue
            name = title.strip()

            description = _strip_html((company.get("content") or {}).get("rendered"))
            website = _extract_website(company.get("profiles"), self._WEBSITE_RE)

            entries.append(
                PortfolioEntry(
                    firm=self.firm,
                    name=name,
                    website=website,
                    description=description,
                    source_url=self.PORTFOLIO_URL,
                )
            )
        return entries


def _strip_html(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = HTMLParser(value).text(strip=True)
    return text or None


def _extract_website(profiles: object, website_re: re.Pattern[str]) -> str | None:
    if not isinstance(profiles, str):
        return None
    match = website_re.search(profiles)
    if not match:
        return None
    candidate = match.group(1).strip()
    parsed = urlparse(candidate)
    # Reject malformed URLs (e.g. FF's "http:///www.spacex.com/" triple-slash).
    if not parsed.scheme or not parsed.netloc:
        return None
    return candidate
