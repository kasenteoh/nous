"""Heuristic detector for parked / for-sale / registrar-placeholder pages.

Why: resolve_homepage accepts any 200 page whose text mentions the company
name — and a parked page ALWAYS mentions the domain name, which is how prod
attached parked 9gag.ai/substack.ai/cameo.ai to real companies. This check
runs before the name-mention acceptance.

Deliberately conservative: a false positive rejects a real company homepage
(expensive — the company stays website-less), while a false negative just
defers to enrichment's website_state signal (cheap). Standalone phrases must
be domain-sale specific; marketplace brand names alone only count when a
sale-intent phrase co-occurs ("powered by GoDaddy" on a real site must not
trip it, and product copy like "list items for sale" has no domain wording).
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

# "<host> is for sale" — a registrar/marketplace lander announcing its own
# domain is for sale ("foodology.com is for sale", "The domain Pinecone.com is
# for sale!"). The _SALE_PHRASES below all require the literal word "domain" (or
# "site"/"website"), so a page that names the bare host slips through and gets
# attached to a real company (a D3 wrong-company match). Requires a dotted host
# ending in an alphabetic TLD immediately before "is for sale", so product copy
# ("list items for sale") and version strings ("v1.0 is for sale") never trip it.
_DOMAIN_FOR_SALE: re.Pattern[str] = re.compile(
    r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}\s+is\s+for\s+sale\b"
)

# Phrases that on their own mark a domain-sale/parking page (lowercase).
_SALE_PHRASES: tuple[str, ...] = (
    "this domain is for sale",
    "domain is for sale",
    "domain name is for sale",
    "domain for sale",
    "domain may be for sale",
    "buy this domain",
    "purchase this domain",
    "is parked free",
    "parked domain",
    "domain parking",
    "domain marketplace",
    # Softer "site / website for sale" language used on custom landers that
    # don't rely on a registrar template (e.g. Foodology-style pages).
    "this site is for sale",
    "this website is for sale",
    "available for purchase",
    "inquire about this domain",
)

# Registrar / domain-marketplace brands: only parked when a sale-intent
# phrase co-occurs (brand names appear in footers of real sites).
_MARKETPLACE_BRANDS: tuple[str, ...] = (
    # domain-qualified entries: bare "spaceship" is a common noun and bare
    # "sedo" substring-matches "Sedona" — both false-positive on real pages
    # with sale-intent copy (space tourism, real estate). Sedo landers carry
    # tier-1 phrases anyway.
    "spaceship.com",
    "godaddy",
    "sedo.com",
    "afternic",
    "dan.com",
    "hugedomains",
    "atom.com",
    "saw.com",
    "squadhelp",
    "namecheap",
    "porkbun",
    "reg.ai",
)

_SALE_INTENT: tuple[str, ...] = ("for sale", "buy now", "make an offer", "make offer")


def looks_parked(html: str) -> bool:
    """True when *html* looks like a parked / for-sale / placeholder page."""
    tree = HTMLParser(html)
    text = " ".join(tree.text(separator=" ").split())
    title_node = tree.css_first("title")
    if title_node is not None:
        title = " ".join(title_node.text(separator=" ").split())
        text = f"{title} {text}"
    return text_looks_parked(text)


def text_looks_parked(text: str) -> bool:
    """True when already-extracted visible *text* reads as a parked/for-sale page.

    The resolver's text-level detector — deliberately lenient (a false positive
    just rejects a candidate homepage, which is cheap). Split out from
    :func:`looks_parked` so it can run on extracted text. The repair backfill uses
    the stricter :func:`page_is_for_sale_lander` instead, because scanning a real
    company's full page text makes these looser signals false-positive.
    """
    lowered = text.lower()
    if _DOMAIN_FOR_SALE.search(lowered):
        return True
    if any(phrase in lowered for phrase in _SALE_PHRASES):
        return True
    return any(brand in lowered for brand in _MARKETPLACE_BRANDS) and any(
        intent in lowered for intent in _SALE_INTENT
    )


# Lander phrases for the repair backfill (page_is_for_sale_lander). A STRICT
# subset of _SALE_PHRASES: only self-referential domain-sale language a real
# company homepage never uses about itself. Excludes the looser resolver phrases
# ("available for purchase", "this site/website is for sale", "domain
# marketplace", "domain for sale") that real pages legitimately carry — a
# cyber-insurer whose product is "available for purchase", a domain-marketplace
# startup, etc. — because the backfill scans full real-page text, not a sparse
# candidate lander.
_STRICT_LANDER_PHRASES: tuple[str, ...] = (
    "this domain is for sale",
    "this domain name is for sale",
    "domain may be for sale",
    "buy this domain",
    "purchase this domain",
    "inquire about this domain",
    "is parked free",
    "parked domain",
    "domain parking",
)


def page_is_for_sale_lander(text: str) -> bool:
    """True when *text* (a stored page's extracted content) announces the page's
    OWN domain is for sale — the strict detector for the repair backfill.

    Stricter than :func:`text_looks_parked` (the resolver's detector) on purpose:
    the backfill re-judges a real company's FULL page text, where the resolver's
    looser signals false-positive (e.g. At-Bay, a cyber-insurer whose product is
    "available for purchase").  A false positive here resets an already-resolved
    real company, so this keys only on a self-referential ``<host> is for sale``
    or an unambiguous lander phrase.
    """
    lowered = text.lower()
    if _DOMAIN_FOR_SALE.search(lowered):
        return True
    return any(phrase in lowered for phrase in _STRICT_LANDER_PHRASES)
