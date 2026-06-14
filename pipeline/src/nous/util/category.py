"""Canonical primary_category taxonomy + normalizer (Task 6.1).

`primary_category` is a second, parallel free-text taxonomy alongside
`industry_group` (M1, `util/industry.py`). The enrichment LLM was told to pick
"a common bucket like 'developer tools', 'fintech', 'AI infrastructure'", but
without a fixed vocabulary it drifted the same way `industry_group` did:
ad-tech / adtech / advertising technology; biotech / biotech tooling; dev tools
/ devtools — one concept fragmenting across several category-filter options and
several thin tag/category pages. This collapses the common variants onto a small
canonical set.

It deliberately mirrors `util/industry.py`: the same canonical vocabulary plus
an alias map, normalized with the same case-/separator-insensitive match key.
Keeping the two modules separate (rather than aliasing one to the other) lets
the category vocabulary diverge later without disturbing the industry filter,
while sharing the proven shape.

Applied by the `normalize-taxonomy` stage, which recanonicalizes existing
`companies.primary_category` in place — a pure string op, so the historical
sprawl heals with no extra LLM cost.

The alias map is a pragmatic starting point, not exhaustive: unrecognised values
pass through trimmed (never dropped), and new aliases are cheap to add.
"""

from __future__ import annotations

import re

CANONICAL_CATEGORIES: list[str] = [
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
]

# Extra freeform spellings → canonical. The canonical terms themselves are added
# automatically in _build_aliases, so only true synonyms/variants belong here.
# Mirrors util/industry.py's alias set, with the extra category-only spellings
# the enrichment prompt invited ("biotech tooling", "AI tooling", ...).
_ALIAS_SOURCES: dict[str, list[str]] = {
    "AI infrastructure": [
        "ai", "a.i.", "artificial intelligence", "ai infra", "ai platform",
        "ai tools", "ai tooling", "ai/ml", "machine learning", "ml",
        "ml infrastructure", "ml platform", "generative ai", "genai",
        "ai research", "ai applications", "applied ai", "ai productivity",
        "ai assistant", "mlops",
    ],
    "developer tools": [
        "devtools", "dev tools", "developer tooling", "developer platform",
        "developer experience", "devops", "developer infrastructure",
        "api tools", "apis", "engineering tools", "infrastructure software",
    ],
    "fintech": [
        "financial technology", "finance", "financial services", "payments",
        "banking", "lending", "insurtech", "insurance", "wealthtech",
        "capital markets", "accounting",
    ],
    "healthcare": [
        "health", "health tech", "healthtech", "digital health",
        "consumer health", "consumer health tech", "medical", "medtech",
        "health care", "telehealth", "mental health",
    ],
    "biotech": [
        "biotechnology", "biotech tooling", "biotech r&d", "life sciences",
        "bio", "drug discovery", "synthetic biology", "genomics", "pharma",
    ],
    "cybersecurity": [
        "security", "cyber security", "infosec", "information security",
        "application security", "cloud security", "data security",
    ],
    "data infrastructure": [
        "data", "data platform", "data infra", "b2b data infrastructure",
        "analytics", "b2b saas analytics", "data analytics", "databases",
        "data engineering", "business intelligence", "observability",
    ],
    "sales & marketing tech": [
        "adtech", "ad tech", "advertising technology", "advertising",
        "martech", "marketing technology", "marketing", "sales tech",
        "sales technology", "crm", "go-to-market", "customer support",
        "customer success",
    ],
    "HR tech": [
        "hr", "human resources", "hrtech", "people ops", "talent",
        "recruiting", "ai recruiting", "talent acquisition", "workforce",
        "hr software",
    ],
    "legal tech": ["legaltech", "legal", "law", "legal software", "compliance"],
    "edtech": [
        "education technology", "education", "edu tech", "learning",
        "corporate learning", "e-learning",
    ],
    "proptech": [
        "property technology", "real estate", "real estate technology",
        "real estate tech", "construction tech", "construction",
    ],
    "logistics & supply chain": [
        "logistics", "supply chain", "supply chain technology", "freight",
        "shipping", "logistics technology", "supply chain management",
    ],
    "robotics": [
        "robots", "robotics & automation", "automation", "autonomous systems",
        "autonomous vehicles", "drones",
    ],
    "hardware": [
        "consumer electronics", "electronics", "ai hardware", "semiconductors",
        "chips", "devices", "iot", "space", "aerospace", "manufacturing",
    ],
    "climate & energy": [
        "climate tech", "cleantech", "clean energy", "climate", "energy",
        "climate / weather tech", "climate and weather tech",
        "renewable energy", "sustainability", "greentech", "energy tech",
    ],
    "consumer": [
        "consumer ai", "consumer apps", "consumer goods", "consumer services",
        "consumer software", "consumer product", "social", "creator tools",
        "creator economy", "consumer tech", "e-commerce", "ecommerce",
        "marketplace", "food", "media", "travel",
    ],
    "gaming": [
        "games", "game development", "blockchain gaming", "video games",
        "game studio",
    ],
    "crypto/web3": [
        "crypto", "web3", "blockchain", "blockchain infrastructure",
        "blockchain security", "defi", "cryptocurrency", "fintech / crypto",
    ],
    "vertical SaaS": [
        "b2b saas", "saas", "enterprise saas", "enterprise software",
        "b2b software", "vertical software", "enterprise", "b2b",
        "productivity", "productivity software", "collaboration",
    ],
}


def _key(value: str) -> str:
    """Mechanical match key: lowercased, separators (space / - / _ / slash)
    collapsed to a single space."""
    return re.sub(r"[\s\-_/]+", " ", value.strip().lower()).strip()


def _build_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for canon in CANONICAL_CATEGORIES:
        aliases[_key(canon)] = canon
    for canon, variants in _ALIAS_SOURCES.items():
        for variant in variants:
            aliases.setdefault(_key(variant), canon)
    return aliases


_ALIASES: dict[str, str] = _build_aliases()


def normalize_category(value: str | None) -> str | None:
    """Map a freeform primary_category label onto CANONICAL_CATEGORIES.

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
