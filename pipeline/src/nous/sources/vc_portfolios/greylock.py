"""Greylock portfolio adapter.

https://greylock.com/portfolio/ is server-rendered: each company is a
``<div class="portfolio-modal-box cropped_modal" id="<slug>">`` containing a
logo (``alt="<Name> Logo"``), a short tagline (``<h2>``), a description
(``<p class="l">``), and a social-link row whose last ``<a>`` (the one
wrapping ``icon-link-dark.svg``) points at the company homepage.

Greylock doesn't print the company name as text in the modal — only the logo
alt attribute and the modal's ``id`` slug carry it. We derive the name from
the logo alt (stripping the trailing " Logo"), falling back to the id slug if
the alt is missing.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.base import PortfolioEntry


class GreylockAdapter:
    firm = "greylock"
    PORTFOLIO_URL = "https://greylock.com/portfolio/"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        tree = HTMLParser(html)
        entries: list[PortfolioEntry] = []
        seen_ids: set[str] = set()
        for modal in tree.css(".portfolio-modal-box.cropped_modal"):
            slug = modal.attributes.get("id") or ""
            if not slug or slug in seen_ids:
                continue
            seen_ids.add(slug)

            name = _extract_name(modal, slug)
            if not name:
                continue
            website = _extract_website(modal)
            desc_node = modal.css_first("p.l")
            description = desc_node.text(strip=True) if desc_node else None
            if description == "":
                description = None
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


def _extract_name(modal: object, slug: str) -> str | None:
    # selectolax's Node — typed loosely to keep this helper test-friendly.
    logo_img = modal.css_first(".logo-area img")  # type: ignore[attr-defined]
    if logo_img is not None:
        alt = (logo_img.attributes.get("alt") or "").strip()
        if alt:
            # Greylock alts are uniformly "<Name> Logo".
            if alt.lower().endswith(" logo"):
                alt = alt[: -len(" logo")].strip()
            if alt:
                return alt
    # Fallback: derive from id slug (e.g. "palo-alto-networks" -> "Palo Alto Networks").
    return slug.replace("-", " ").title() if slug else None


def _extract_website(modal: object) -> str | None:
    for anchor in modal.css(".social-link a"):  # type: ignore[attr-defined]
        img = anchor.css_first("img")
        if img is None:
            continue
        src = img.attributes.get("src") or ""
        if "icon-link-dark" in src:
            href = anchor.attributes.get("href")
            if isinstance(href, str) and href.strip():
                return href.strip()
    return None
