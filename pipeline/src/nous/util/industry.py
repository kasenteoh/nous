"""Canonical industry taxonomy + normalizer (M1).

The `industry_group` filter on /companies had drifted to ~264 freeform LLM
values (ad-tech / adtech / advertising technology; climate tech / climate-tech /
cleantech; AI infrastructure / AI research / ...), so one concept fragmented
across several filter options and hid matches. This collapses the common
variants onto a small canonical set.

Two layers use it:
  - the company-description prompt steers the LLM toward CANONICAL_INDUSTRIES, and
  - the enrich stage normalizes industry_group on (re-)enrichment — a pure
    string op, so the historical sprawl heals as the cron re-enriches, with no
    extra LLM cost.

The alias map is a pragmatic starting point, not exhaustive: unrecognised values
pass through trimmed (never dropped), and new aliases are cheap to add.
"""

from __future__ import annotations

import re

CANONICAL_INDUSTRIES: list[str] = [
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
_ALIAS_SOURCES: dict[str, list[str]] = {
    "AI infrastructure": [
        "ai", "a.i.", "artificial intelligence", "ai infra", "ai platform",
        "ai tools", "ai tooling", "ai/ml", "machine learning", "ml",
        "ml infrastructure", "generative ai", "genai", "ai research",
        "ai applications", "applied ai", "ai productivity", "ai assistant",
    ],
    "developer tools": [
        "devtools", "developer platform", "developer experience", "devops",
        "developer infrastructure", "api tools", "engineering tools",
    ],
    "fintech": [
        "financial technology", "finance", "financial services", "payments",
        "banking", "lending", "insurtech", "wealthtech", "capital markets",
    ],
    "healthcare": [
        "health", "health tech", "healthtech", "digital health",
        "consumer health", "consumer health tech", "medical", "medtech",
        "health care", "telehealth",
    ],
    "biotech": [
        "biotechnology", "biotech tooling", "life sciences", "bio",
        "drug discovery", "synthetic biology", "biotech r&d",
    ],
    "cybersecurity": [
        "security", "cyber security", "infosec", "information security",
        "application security", "cloud security",
    ],
    "data infrastructure": [
        "data", "data platform", "data infra", "b2b data infrastructure",
        "analytics", "b2b saas analytics", "data analytics", "databases",
        "data engineering",
    ],
    "sales & marketing tech": [
        "adtech", "ad tech", "advertising technology", "advertising",
        "martech", "marketing technology", "marketing", "sales tech",
        "sales technology", "crm", "go-to-market",
    ],
    "HR tech": [
        "hr", "human resources", "hrtech", "people ops", "talent",
        "recruiting", "ai recruiting", "talent acquisition", "workforce",
    ],
    "legal tech": ["legaltech", "legal", "law", "legal software"],
    "edtech": [
        "education technology", "education", "edu tech", "learning",
        "corporate learning",
    ],
    "proptech": [
        "property technology", "real estate", "real estate technology",
        "real estate tech",
    ],
    "logistics & supply chain": [
        "logistics", "supply chain", "supply chain technology", "freight",
        "shipping", "logistics technology",
    ],
    "robotics": [
        "robots", "robotics & automation", "automation", "autonomous systems",
        "autonomous vehicles",
    ],
    "hardware": [
        "consumer electronics", "electronics", "ai hardware", "semiconductors",
        "chips", "devices", "iot",
    ],
    "climate & energy": [
        "climate tech", "cleantech", "clean energy", "climate", "energy",
        "climate / weather tech", "climate and weather tech",
        "renewable energy", "sustainability", "greentech",
    ],
    "consumer": [
        "consumer ai", "consumer apps", "consumer goods", "consumer services",
        "consumer software", "consumer product", "social", "creator tools",
        "creator economy",
    ],
    "gaming": ["games", "game development", "blockchain gaming", "video games"],
    "crypto/web3": [
        "crypto", "web3", "blockchain", "blockchain infrastructure",
        "blockchain security", "defi", "cryptocurrency",
    ],
    "vertical SaaS": [
        "b2b saas", "saas", "enterprise saas", "enterprise software",
        "b2b software", "vertical software",
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
