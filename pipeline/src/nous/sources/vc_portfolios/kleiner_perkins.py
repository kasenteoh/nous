"""Kleiner Perkins portfolio adapter.

Kleiner's "Our Companies" page (https://www.kleinerperkins.com/partnerships/) is
a JS-rendered grid whose company names aren't present as parseable anchors in
the served HTML. The WordPress ``company`` post type is, however, fully
enumerated in https://www.kleinerperkins.com/company-sitemap.xml as
``/company/<slug>/`` URLs (~400 companies). We read the sitemap and derive each
name from its slug (e.g. "bedrock-systems" -> "Bedrock Systems"), mirroring the
slug-fallback Greylock uses. The listing exposes no homepage URL, so ``website``
is ``None`` and resolve-homepages fills it in later. ``source_url`` is the
human-facing portfolio page.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.base import PortfolioEntry

_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)


class KleinerPerkinsAdapter:
    firm = "kleiner_perkins"
    PORTFOLIO_URL = "https://www.kleinerperkins.com/partnerships/"
    SITEMAP_URL = "https://www.kleinerperkins.com/company-sitemap.xml"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        xml = (await client.fetch(self.SITEMAP_URL)).content
        entries: list[PortfolioEntry] = []
        seen: set[str] = set()
        for loc in _LOC_RE.findall(xml):
            slug = _company_slug(loc)
            if slug is None:
                continue
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
        if not entries:
            raise RuntimeError(
                "kleiner_perkins: no /company/<slug> URLs found in company-sitemap.xml; "
                "the sitemap structure likely changed."
            )
        return entries


def _company_slug(loc: str) -> str | None:
    """Return the slug for a ``/company/<slug>/`` URL, else None."""
    parts = [p for p in urlparse(loc.strip()).path.split("/") if p]
    if len(parts) == 2 and parts[0] == "company":
        return parts[1]
    return None


def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").title()
