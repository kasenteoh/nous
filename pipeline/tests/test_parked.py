"""Unit tests for the parked/for-sale page detector.

False positives are expensive (a real company website rejected); false
negatives are cheap (enrichment's website_state catches them later). The
detector is therefore deliberately conservative.
"""

from __future__ import annotations

from nous.sources.parked import looks_parked, text_looks_parked

SPACESHIP_PARKED = """
<html><head><title>9gag.ai is for sale</title></head><body>
<h1>9gag.ai</h1><p>This domain is for sale. Get it before someone else does.</p>
<a href="#">Buy now on Spaceship</a></body></html>
"""

GODADDY_PARKED = """
<html><head><title>cameo.ai</title></head><body>
<p>cameo.ai is parked free, courtesy of GoDaddy.com.</p>
<p>Would you like to buy this domain?</p></body></html>
"""

MARKETPLACE_PARKED = """
<html><head><title>Premium domain</title></head><body>
<p>The domain name enter.ai is for sale! Make an offer via Atom.com,
the leading domain marketplace.</p></body></html>
"""

REAL_HOMEPAGE = """
<html><head><title>Acme — ship faster</title></head><body>
<nav>Product Pricing About</nav>
<h1>Acme helps engineering teams ship faster</h1>
<p>Trusted by 400 companies. Read our customer stories.</p></body></html>
"""

# A real product whose copy mentions listing items for sale (the SellRaze
# case that a naive "for sale" pattern false-matched in prod analysis).
ECOMMERCE_HOMEPAGE = """
<html><head><title>SellRaze</title></head><body>
<h1>List items for sale across every marketplace</h1>
<p>SellRaze uses image recognition to identify, price, and list your items
for sale on eBay, Amazon, and more.</p></body></html>
"""


def test_detects_spaceship_style_sale_page() -> None:
    assert looks_parked(SPACESHIP_PARKED) is True


def test_detects_godaddy_parking() -> None:
    assert looks_parked(GODADDY_PARKED) is True


def test_detects_marketplace_listing() -> None:
    assert looks_parked(MARKETPLACE_PARKED) is True


def test_real_homepage_not_parked() -> None:
    assert looks_parked(REAL_HOMEPAGE) is False


def test_ecommerce_copy_mentioning_for_sale_not_parked() -> None:
    assert looks_parked(ECOMMERCE_HOMEPAGE) is False


BRAND_FOOTER_HOMEPAGE = """
<html><head><title>Acme Store</title></head><body>
<h1>Acme — handmade goods</h1>
<p>Browse our catalog and find something you love.</p>
<footer>Website powered by GoDaddy. Domain registered with Namecheap.</footer>
</body></html>
"""


def test_brand_mention_without_sale_intent_not_parked() -> None:
    assert looks_parked(BRAND_FOOTER_HOMEPAGE) is False


TITLE_ONLY_PARKED = """
<html><head><title>example.com — this domain is for sale</title></head>
<body><div id="app"></div></body></html>
"""


def test_title_only_sale_signal_detected() -> None:
    assert looks_parked(TITLE_ONLY_PARKED) is True


# Styled lander whose sale phrase is split across an element boundary AND an
# &nbsp; — without separator+collapse normalization the text jams to
# "this domainisfor sale" and no phrase matches. Deliberately tier-1-only
# (no marketplace brand) so this test pins the normalization, not co-occurrence.
STYLED_DOMAIN_PARKED = """
<html><head><title>Premium domain</title></head><body>
<p>This domain&nbsp;<span>is</span> for sale. Submit your best offer today.</p>
</body></html>
"""


def test_element_boundary_and_nbsp_normalized() -> None:
    assert looks_parked(STYLED_DOMAIN_PARKED) is True


# A registrar lander whose ONLY sale signal is "<host> is for sale" — no literal
# word "domain", no marketplace brand. This is the Foodology shape: a namesake
# for-sale page ("foodology.com is for sale") the resolver accepted because the
# body also carries real-looking prose that mentions the company name. Every
# pre-existing _SALE_PHRASE requires the word "domain", so this slipped through.
HOST_FOR_SALE_LANDER = """
<html><head><title>Exploring Culinary Delights with Foodology</title></head><body>
<p>foodology.com is for sale.</p>
<h1>Exploring Culinary Delights with Foodology</h1>
<p>Discovering global culinary traditions, techniques, and sustainable sourcing.</p>
</body></html>
"""


def test_detects_bare_host_for_sale() -> None:
    # "<host> is for sale" with no "domain" wording and no marketplace brand.
    assert looks_parked(HOST_FOR_SALE_LANDER) is True


def test_text_looks_parked_matches_extracted_content() -> None:
    # The repair backfill re-judges RawPage.content — already-extracted visible
    # text (with the <title> prepended), not raw HTML — so the detector must work
    # on plain text too. This is exactly what prod scraped for foodology.com.
    content = (
        "foodology.com is for sale.\n\nExploring Culinary Delights with Foodology\n\n"
        "Discovering Global Culinary Traditions. The world of food is as diverse "
        "and fascinating as the cultures it represents."
    )
    assert text_looks_parked(content) is True


def test_text_looks_parked_ignores_product_for_sale_copy() -> None:
    # Real product copy: the subject of "for sale" is "items", not a domain, and
    # there is no marketplace brand. Must NOT trip (the SellRaze false positive).
    content = (
        "SellRaze | The fastest way to sell your stuff\n"
        "List items for sale across every marketplace using image recognition."
    )
    assert text_looks_parked(content) is False
