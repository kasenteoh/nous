"""Canonical industry taxonomy + normalizer (M1; expanded for the long tail).

The `industry_group` filter on /companies drifted to a freeform LLM vocabulary —
a prod snapshot showed **273 distinct values across ~1,250 enriched companies,
~61% of them singletons** (healthcare / healthtech / healthcare technology /
healthcare AI / healthcare software as five separate filter options; ad-tech /
adtech / advertising technology; e-commerce / ecommerce / ecommerce SaaS). One
concept fragmented across several dropdown entries and hid matches.

The original M1 alias map (~20 buckets / ~120 aliases) collapsed the common
spellings but let the long tail pass through raw, so ~196 distinct values still
leaked into the dropdown. This expansion settles a **34-bucket** canonical set
(the original 20 plus 14 buckets the data forced: defense & aerospace,
e-commerce & retail, manufacturing & industrial, food & beverage, media &
entertainment, government & public sector, transportation & mobility, agtech,
enterprise infrastructure, identity & fraud) and grows the alias map to map
**~99% of the observed raw values** onto a canonical bucket. The handful left to
pass through are genuinely idiosyncratic one-offs (e.g. "diving equipment"),
which is the intended behaviour — better an un-canonicalised bucket than a lost
one.

Two layers use it:
  - the company-description prompt steers the LLM toward CANONICAL_INDUSTRIES, and
  - the `normalize-taxonomy` stage (and enrichment) rewrites `industry_group`
    via `normalize_industry` — a pure string op, so the historical sprawl heals
    with no extra LLM cost.

Judgement calls worth flagging for review (the map is committed and reviewed in
the PR):
  - "e-commerce & retail" is split out from "consumer": storefront/marketplace
    tooling is a distinct buyer from consumer apps/brands.
  - "manufacturing & industrial" is split from "hardware": factory/industrial
    software ≠ physical devices/chips.
  - "defense & aerospace" absorbs space, aviation, satellites and public-safety
    tooling — adjacent gov/dual-use buyers that don't fit "hardware".
  - travel ("travel tech") folds into "consumer" (consumer-facing booking),
    rather than its own thin bucket (only a few companies).
  - quantum computing → "AI infrastructure" (frontier-compute substrate; too few
    to merit its own bucket yet).
  - hospitality / restaurant / events tooling → "vertical SaaS" (industry
    software), keeping "food & beverage" for food *products*/delivery.

Unrecognised values pass through trimmed (never dropped); new aliases are cheap
to add.
"""

from __future__ import annotations

import re

CANONICAL_INDUSTRIES: list[str] = [
    # Original M1 set (kept verbatim — the stable base of the taxonomy).
    "AI infrastructure",
    "developer tools",
    "fintech",
    "healthcare",
    "biotech",
    "cybersecurity",
    "data infrastructure",
    "sales & marketing tech",
    "HR tech",
    "legal tech",
    "edtech",
    "proptech",
    "logistics & supply chain",
    "robotics",
    "hardware",
    "climate & energy",
    "consumer",
    "gaming",
    "crypto/web3",
    "vertical SaaS",
    # Added for the observed long tail (each backs a real multi-company cluster
    # except where noted, and pulls a swathe of singletons out of the dropdown).
    "defense & aerospace",
    "e-commerce & retail",
    "manufacturing & industrial",
    "food & beverage",
    "media & entertainment",
    "government & public sector",
    "transportation & mobility",
    "agtech",
    "enterprise infrastructure",
    "identity & fraud",
]

# Extra freeform spellings → canonical. The canonical terms themselves are added
# automatically in _build_aliases, so only true synonyms/variants belong here.
# Built to cover every distinct industry_group value seen in prod (≈99%); the
# residue is deliberate idiosyncratic passthrough.
_ALIAS_SOURCES: dict[str, list[str]] = {
    "AI infrastructure": [
        "ai", "a.i.", "artificial intelligence", "ai infra", "ai platform",
        "ai tools", "ai tooling", "ai/ml", "machine learning", "ml",
        "ml infrastructure", "generative ai", "genai", "ai research",
        "ai applications", "applied ai", "ai productivity", "ai assistant",
        "ai consulting", "ai consulting and implementation", "ai directory",
        "ai governance", "ai video generation", "enterprise conversational ai",
        # Frontier compute substrate — too few companies to merit its own bucket.
        "quantum computing",
    ],
    "developer tools": [
        "devtools", "developer platform", "developer experience", "devops",
        "developer infrastructure", "api tools", "engineering tools", "apis",
        "software", "software services", "internet software",
    ],
    "fintech": [
        "financial technology", "finance", "financial services", "payments",
        "banking", "lending", "insurtech", "wealthtech", "capital markets",
        "insurance", "accounting", "cyber insurance", "fintech infrastructure",
        # Insurance/payments rails for health — financial buyer, not clinical.
        "health insurance technology", "healthcare fintech",
    ],
    "healthcare": [
        "health", "health tech", "healthtech", "digital health",
        "consumer health", "consumer health tech", "medical", "medtech",
        "health care", "telehealth", "healthcare technology", "healthcare it",
        "healthcare ai", "healthcare software", "mental health",
        "health and wellness", "health & wellness", "employee wellness",
        "fitness technology", "medical device", "molecular diagnostics",
        # Consumer health-adjacent; lands closer to healthcare than to consumer.
        "petcare",
    ],
    "biotech": [
        "biotechnology", "biotech tooling", "life sciences", "bio",
        "drug discovery", "synthetic biology", "biotech r&d",
        "life sciences ai", "life sciences tooling",
    ],
    "cybersecurity": [
        "security", "cyber security", "infosec", "information security",
        "application security", "cloud security", "data security",
        "cybersecurity training", "physical security", "privacy tech",
        # Crypto/web3 *security* posture is a security buyer first.
        "web3 security", "crypto infrastructure", "crypto / defi",
    ],
    "data infrastructure": [
        "data", "data platform", "data infra", "b2b data infrastructure",
        "analytics", "b2b saas analytics", "data analytics", "databases",
        "data engineering", "market research", "user research software",
        "geospatial / mapping", "positioning technology",
    ],
    "sales & marketing tech": [
        "adtech", "ad tech", "advertising technology", "advertising",
        "martech", "marketing technology", "marketing", "sales tech",
        "sales technology", "crm", "go-to-market", "sales automation",
        "sales enablement", "sales intelligence", "sales and marketing",
        "marketing analytics", "marketing automation", "social media marketing",
        "customer engagement", "crypto marketing",
    ],
    "HR tech": [
        "hr", "human resources", "hrtech", "people ops", "talent",
        "recruiting", "ai recruiting", "talent acquisition", "workforce",
        "hr technology", "human resources technology", "job search",
    ],
    "legal tech": [
        "legaltech", "legal", "law", "legal software", "legal technology",
        # Reg/compliance tooling is the same legal-ops buyer.
        "regtech", "compliance software", "compliance",
    ],
    "edtech": [
        "education technology", "education", "edu tech", "learning",
        "corporate learning", "enterprise learning",
    ],
    "proptech": [
        "property technology", "real estate", "real estate technology",
        "real estate tech", "prop tech", "construction",
        "construction technology", "construction tech", "architecture-technology",
        "architecture, engineering, and construction (aec) software",
        "urban development", "home services", "home services software",
        "smart home", "infrastructure inspection",
    ],
    "logistics & supply chain": [
        "logistics", "supply chain", "supply chain technology", "freight",
        "shipping", "logistics technology", "logistics automation",
        "supply chain software", "supply chain management",
        "supply chain & logistics", "procurement software",
    ],
    "robotics": [
        "robots", "robotics & automation", "automation", "autonomous systems",
        "autonomous vehicles", "drones",
    ],
    "hardware": [
        "consumer electronics", "electronics", "ai hardware", "semiconductors",
        "chips", "devices", "iot", "semiconductor", "iot hardware",
        "hardware engineering tools", "hardware robotics", "hardware support",
        "networking", "data center infrastructure",
    ],
    "climate & energy": [
        "climate tech", "cleantech", "clean energy", "climate", "energy",
        "climate / weather tech", "climate and weather tech",
        "renewable energy", "sustainability", "greentech", "energy management",
        "energy storage", "power infrastructure", "environmental technology",
        "critical minerals",
    ],
    "consumer": [
        "consumer ai", "consumer apps", "consumer goods", "consumer services",
        "consumer software", "consumer product", "social", "creator tools",
        "creator economy", "social media", "apparel", "consumer apparel",
        "beauty-tech", "event services", "event ticketing",
        "science crowdfunding",
        # Travel folds here (consumer booking) rather than a thin own bucket.
        "travel", "travel tech", "travel technology",
    ],
    "gaming": [
        "games", "game development", "blockchain gaming", "video games",
        "mobile gaming", "online gaming",
    ],
    "crypto/web3": [
        "crypto", "web3", "blockchain", "blockchain infrastructure",
        "blockchain security", "defi", "cryptocurrency", "nft marketplace",
        # Domain-name marketplaces sit in the web3/naming world here.
        "domain marketplace", "domain services", "web3 / music",
    ],
    "vertical SaaS": [
        "b2b saas", "saas", "enterprise saas", "enterprise software",
        "b2b software", "vertical software", "productivity",
        "productivity software", "collaboration software", "design tools",
        "creative tools", "creative software", "professional services",
        "consulting", "it-consulting", "it services", "it operations",
        "digital adoption platform", "website builder", "publishing tools",
        "video editing software", "engineering software",
        "customer service software", "customer support", "customer-ops",
        "sports technology", "venture capital", "venture capital software",
        "venture studio",
        # Industry software for hospitality/food-service venues (the buyer is a
        # business); food *products*/delivery stay under "food & beverage".
        "hospitality", "hospitality technology", "restaurant technology",
    ],
    "defense & aerospace": [
        "defense", "defense technology", "defense ai", "defense and intelligence",
        "defense and aviation software", "aerospace", "aerospace manufacturing",
        "space", "space technology", "space infrastructure", "aviation",
        "satellite intelligence", "public safety", "public safety technology",
    ],
    "e-commerce & retail": [
        "e-commerce", "ecommerce", "e-commerce software", "e-commerce tools",
        "ecommerce analytics", "ecommerce saas", "ecommerce software", "retail",
        "retail technology", "marketplace",
    ],
    "manufacturing & industrial": [
        "manufacturing", "manufacturing software", "industrial automation",
        "industrial ai", "industrial technology", "electronics manufacturing",
        "hardware manufacturing",
    ],
    "food & beverage": [
        "food", "food tech", "food technology", "food-tech", "food delivery",
        "food distribution", "food & beverage", "food and beverage",
        "food & beverage technology",
    ],
    "media & entertainment": [
        "media", "media & entertainment", "entertainment",
        "entertainment technology", "content media", "music", "music tech",
        "music technology", "publishing",
    ],
    "government & public sector": [
        "govtech", "government technology", "government services", "civic tech",
    ],
    "transportation & mobility": [
        "transportation", "transportation tech", "electric vehicles",
        "micro-mobility", "telecommunications",
    ],
    "agtech": ["agriculture", "agtech", "agriculture technology"],
    "enterprise infrastructure": [
        "cloud infrastructure", "enterprise automation", "enterprise-networking",
    ],
    "identity & fraud": [
        "identity", "identity management", "identity verification",
        "fraud detection", "fraud prevention",
    ],
}


def _key(value: str) -> str:
    """Mechanical match key: lowercased, separators (space / - / _ / slash)
    collapsed to a single space."""
    return re.sub(r"[\s\-_/]+", " ", value.strip().lower()).strip()


def _build_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for canon in CANONICAL_INDUSTRIES:
        aliases[_key(canon)] = canon
    for canon, variants in _ALIAS_SOURCES.items():
        for variant in variants:
            aliases.setdefault(_key(variant), canon)
    return aliases


_ALIASES: dict[str, str] = _build_aliases()


def normalize_industry(value: str | None) -> str | None:
    """Map a freeform industry label onto CANONICAL_INDUSTRIES.

    Canonical terms and known aliases (case- and separator-insensitive) collapse
    to their canonical display form. Unrecognised values pass through trimmed —
    better an un-canonicalised bucket than a lost one. None / blank → None.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return _ALIASES.get(_key(stripped), stripped)
