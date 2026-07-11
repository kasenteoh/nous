"""Canonical tag vocabulary + canonicalizer (H-2, 2026-07-11).

The judge prompt's open tag vocabulary produces near-synonym fragmentation:
the first live golden-set re-recording (branch ``eval-record/20260711-081233``)
scored tags_f1 0.265 against hand-authored fixtures, with mismatches like
``ci-observability`` vs ``ci-cd``, ``payment-routing`` vs ``payments``, and
``wholesale-marketplace`` vs ``marketplace`` — the same concept spelled two
ways. On the site, every distinct spelling becomes its own thin ``/tag/*``
page, so the fragmentation feeds a long tail of single-company tag pages.

This module is the tags twin of ``util/industry.py`` / ``util/category.py``:
a committed canonical vocabulary plus an alias map, applied as a pure string
op wherever tags are written (enrich) and re-applied over existing rows by
the ``normalize-taxonomy`` stage. The map CONSOLIDATES, it does not gate:
unknown tags pass through mechanically normalized (lowercase, hyphenated) but
otherwise unchanged, so the vocabulary stays open — better an
un-canonicalised tag than a lost one.

Seeding (deliberately generic; ~95 canonical tags):
  - every tag in the golden fixtures' expected + recorded sets
    (``tests/golden/company_description``), keeping the generic side of each
    internal near-duplicate (``marketplaces`` → ``marketplace``,
    ``developer-tools`` → ``devtools``, ``payment-orchestration`` →
    ``payments``, ``listings`` → ``directory``, ``on-call`` →
    ``incident-management``, ``chat`` → ``messaging``);
  - both sides of every live-recording mismatch the 2026-07-11 evals surfaced
    (``ci-observability`` → ``ci-cd``, ``payment-routing`` → ``payments``,
    ``wholesale-marketplace`` → ``marketplace``);
  - the tag examples visible in prompts and tests (api / cloud / saas /
    open-source / ...); and
  - sensible generic families (ai, ml, devtools, ci-cd, observability,
    security, fintech, payments, healthcare, logistics, marketplace, saas,
    api, infrastructure, data, analytics, open-source, ...).

Judgement calls worth flagging for review:
  - ``devtools`` is the canonical spelling (over ``developer-tools``) — the
    shorter form is the established community term and slug.
  - ``crypto`` absorbs web3 / blockchain / defi: one concept-family, one tag
    page, mirroring the industry map's single crypto/web3 bucket.
  - ``observability`` absorbs monitoring / apm / telemetry / tracing /
    logging — the modern umbrella term.
  - ``logistics`` absorbs freight / shipping / delivery; ``supply-chain``
    stays separate (procurement-side vs movement-side buyers).
  - ``billing`` folds into ``payments`` (adjacent money-movement tooling).
  - vendor-specific tags (aws, gcp, azure) and genuinely specific ones
    (hipaa, journaling, dev-boards) deliberately pass through — mapping them
    to a family would erase real information.
  - ``wellness`` / ``music`` / ``esports`` are NOT mapped — each is a real
    standalone concept, not a spelling variant of a canonical tag.

Unlike the industry/category maps there is no fixed display-form list the
LLM must choose from: the judge prompt only *prefers* established tags (with
examples), and this map heals the synonyms it produces anyway. No cap is
applied here — the runtime write path has never truncated tags (the "max ~8"
in the prompt is advisory), and this module preserves that behaviour.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Canonical tags: lowercase-hyphenated, deliberately generic (~95 entries).
# Grouped by family for review; the list order carries no meaning.
CANONICAL_TAGS: list[str] = [
    # AI / ML
    "ai",
    "ml",
    "llm",
    "nlp",
    "computer-vision",
    "ai-agents",
    "mlops",
    "inference",
    "fine-tuning",
    "gpu",
    # Data
    "data",
    "analytics",
    "data-engineering",
    "data-infrastructure",
    "databases",
    "search",
    "finops",
    # Developer tooling
    "devtools",
    "api",
    "sdk",
    "open-source",
    "ci-cd",
    "devops",
    "testing",
    "observability",
    "sre",
    "incident-management",
    # Infrastructure
    "infrastructure",
    "cloud",
    "kubernetes",
    "serverless",
    "automation",
    "low-code",
    # Software / product shapes
    "web-development",
    "mobile-development",
    "mobile-app",
    "saas",
    "enterprise-software",
    "b2b",
    "consumer",
    "productivity",
    "collaboration",
    "project-management",
    "design",
    "messaging",
    "video",
    # Security
    "security",
    "identity",
    "fraud-detection",
    "privacy",
    "compliance",
    # Fintech
    "fintech",
    "payments",
    "banking",
    "lending",
    "insurance",
    "investing",
    "accounting",
    "payroll",
    "crypto",
    # Health
    "healthcare",
    "mental-health",
    "biotech",
    # Verticals & business models
    "legal",
    "edtech",
    "proptech",
    "construction",
    "hr",
    "recruiting",
    "logistics",
    "supply-chain",
    "marketplace",
    "e-commerce",
    "retail",
    "climate",
    "energy",
    "travel",
    "food",
    "restaurants",
    "gaming",
    "media",
    "adtech",
    "marketing",
    "sales",
    "crm",
    "customer-support",
    "erp",
    "agency",
    "consulting",
    "directory",
    "coaching",
    "courses",
    "robotics",
    "iot",
    "embedded",
    "hardware",
]

# Freeform spellings → canonical. The canonical tags themselves are added
# automatically in _build_aliases, so only true synonyms/variants belong here.
# Matching is case- and separator-insensitive (see _key), so one spelling per
# separator family suffices ("ad-tech" also matches "ad tech" / "ad_tech").
_ALIAS_SOURCES: dict[str, list[str]] = {
    "ai": [
        "artificial-intelligence", "generative-ai", "genai", "ai-platform",
        "ai-powered", "ai-tools",
    ],
    "ml": ["machine-learning", "deep-learning", "ml-platform"],
    "llm": ["llms", "large-language-models"],
    "nlp": ["natural-language-processing"],
    "computer-vision": ["image-recognition"],
    "ai-agents": [
        "agents", "agentic-ai", "ai-assistant", "ai-assistants",
        "autonomous-agents",
    ],
    "mlops": ["ml-ops", "llmops"],
    "inference": ["model-serving", "model-inference"],
    "fine-tuning": ["finetuning"],
    "gpu": ["gpus", "gpu-computing", "gpu-cloud"],
    "data": ["big-data"],
    "analytics": [
        "data-analytics", "business-intelligence", "bi", "product-analytics",
        "web-analytics",
    ],
    "data-engineering": ["etl", "data-pipelines", "data-pipeline"],
    "data-infrastructure": ["data-platform", "data-infra"],
    "databases": ["database"],
    "search": ["search-engine", "enterprise-search", "semantic-search"],
    "finops": [
        "cloud-cost", "cloud-cost-optimization", "cloud-cost-management",
        "cost-optimization",
    ],
    "devtools": [
        "developer-tools", "dev-tools", "developer-platform",
        "developer-experience", "dx", "developer-productivity",
        "engineering-tools",
    ],
    "api": ["apis", "api-first", "api-platform", "rest-api", "api-development"],
    "sdk": ["sdks"],
    "open-source": ["opensource", "oss"],
    "ci-cd": [
        # "ci-cd" itself keys as "ci cd"; the run-together spelling needs its
        # own alias. "ci-observability" was a live-recording near-synonym.
        "cicd", "ci", "continuous-integration", "continuous-delivery",
        "continuous-deployment", "ci-observability",
    ],
    "devops": ["platform-engineering"],
    "testing": [
        "test-automation", "qa", "quality-assurance", "software-testing",
        "e2e-testing",
    ],
    "observability": [
        "monitoring", "apm", "telemetry", "tracing", "logging",
        "application-monitoring",
    ],
    "sre": ["site-reliability", "site-reliability-engineering", "reliability"],
    "incident-management": ["on-call", "oncall", "incident-response"],
    "infrastructure": ["infra", "cloud-infrastructure", "it-infrastructure"],
    "cloud": ["cloud-computing", "cloud-native", "multi-cloud", "cloud-platform"],
    "kubernetes": ["k8s", "containers", "docker", "container-orchestration"],
    "automation": [
        "workflow-automation", "rpa", "process-automation",
        "business-automation",
    ],
    "low-code": ["no-code", "nocode", "lowcode"],
    "web-development": ["web-dev", "website-development"],
    "mobile-development": ["mobile-dev", "app-development"],
    "mobile-app": ["mobile-apps", "mobile"],
    "saas": [
        "b2b-saas", "enterprise-saas", "saas-platform",
        "software-as-a-service",
    ],
    "enterprise-software": ["enterprise", "enterprise-tech"],
    "consumer": ["b2c", "consumer-apps", "consumer-tech"],
    "productivity": ["productivity-tools", "productivity-software"],
    "collaboration": ["team-collaboration", "collaboration-tools"],
    "project-management": ["task-management", "work-management"],
    "design": ["design-tools", "ui-ux", "ux", "ui-design", "graphic-design"],
    "messaging": ["chat", "communications", "communication"],
    "video": ["video-streaming", "video-editing", "video-platform", "streaming"],
    "security": [
        "cybersecurity", "infosec", "information-security",
        "application-security", "appsec", "cloud-security", "data-security",
        "network-security", "endpoint-security", "devsecops",
    ],
    "identity": [
        "identity-management", "iam", "authentication", "sso",
        "identity-verification", "access-management",
    ],
    "fraud-detection": ["fraud-prevention", "fraud", "anti-fraud"],
    "privacy": ["data-privacy", "privacy-tech"],
    "compliance": ["regtech", "grc", "regulatory-compliance"],
    "fintech": ["financial-technology", "financial-services", "finance"],
    "payments": [
        # "payment-routing" was a live-recording near-synonym of "payments".
        "payment-processing", "payment-routing", "payment-orchestration",
        "payment-infrastructure", "payouts", "billing",
    ],
    "banking": ["neobank", "digital-banking", "banking-as-a-service"],
    "lending": ["loans", "credit", "lending-platform"],
    "insurance": ["insurtech"],
    "investing": [
        "wealth-management", "wealthtech", "trading", "investment",
        "asset-management",
    ],
    "accounting": ["bookkeeping", "accounting-software", "tax"],
    "crypto": [
        "cryptocurrency", "web3", "blockchain", "defi", "digital-assets",
        "nft",
    ],
    "healthcare": [
        "healthtech", "health", "digital-health", "medtech", "telehealth",
        "telemedicine", "medical",
    ],
    "mental-health": ["behavioral-health"],
    "biotech": [
        "biotechnology", "life-sciences", "drug-discovery", "genomics",
        "synthetic-biology", "pharma",
    ],
    "legal": ["legaltech", "law", "legal-software"],
    "edtech": [
        "education", "e-learning", "elearning", "learning", "online-learning",
        "education-technology",
    ],
    "proptech": ["real-estate", "property-technology", "property-management"],
    "construction": ["construction-tech", "contech"],
    "hr": [
        "hrtech", "human-resources", "people-ops", "hris", "hr-software",
        "workforce-management",
    ],
    "recruiting": ["recruitment", "hiring", "talent-acquisition", "talent", "ats"],
    "logistics": [
        "freight", "shipping", "delivery", "last-mile-delivery",
        "transportation", "fleet-management",
    ],
    "supply-chain": ["supply-chain-management", "procurement"],
    "marketplace": [
        # "wholesale-marketplace" was a live-recording near-synonym.
        "marketplaces", "wholesale-marketplace", "b2b-marketplace",
        "online-marketplace", "two-sided-marketplace",
    ],
    "e-commerce": [
        "ecommerce", "online-retail", "e-commerce-platform", "d2c", "dtc",
        "direct-to-consumer",
    ],
    "retail": ["retail-tech", "retail-technology"],
    "climate": [
        "climate-tech", "cleantech", "sustainability", "greentech", "carbon",
        "decarbonization",
    ],
    "energy": [
        "clean-energy", "renewable-energy", "renewables", "solar",
        "energy-storage", "energy-management",
    ],
    "travel": ["travel-tech", "hospitality"],
    "food": ["food-tech", "foodtech", "food-delivery", "food-and-beverage"],
    "restaurants": ["restaurant", "restaurant-tech"],
    "gaming": ["games", "game-development", "video-games"],
    "media": ["entertainment", "digital-media", "publishing"],
    "adtech": ["advertising", "ads", "programmatic-advertising"],
    "marketing": [
        "martech", "marketing-technology", "marketing-automation",
        "email-marketing", "seo", "growth-marketing", "digital-marketing",
        "social-media-marketing",
    ],
    "sales": [
        "sales-tech", "sales-enablement", "sales-automation",
        "sales-intelligence", "go-to-market", "revenue-operations", "revops",
    ],
    "crm": ["customer-relationship-management"],
    "customer-support": [
        "customer-service", "customer-success", "support", "help-desk",
        "helpdesk", "customer-experience", "cx", "customer-engagement",
    ],
    "erp": ["enterprise-resource-planning"],
    "agency": ["digital-agency", "dev-agency"],
    "consulting": ["consultancy", "professional-services"],
    "directory": ["listings", "business-directory", "local-listings"],
    "courses": ["online-courses"],
    "robotics": ["robots"],
    "iot": ["internet-of-things", "connected-devices", "smart-devices"],
    "embedded": ["embedded-systems", "firmware"],
    "hardware": ["electronics", "consumer-electronics", "semiconductors", "chips"],
}


def _key(value: str) -> str:
    """Mechanical match key: lowercased, separators (space / - / _ / slash)
    collapsed to a single space. Same scheme as util/industry.py."""
    return re.sub(r"[\s\-_/]+", " ", value.strip().lower()).strip()


def _build_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for canon in CANONICAL_TAGS:
        aliases[_key(canon)] = canon
    for canon, variants in _ALIAS_SOURCES.items():
        for variant in variants:
            aliases.setdefault(_key(variant), canon)
    return aliases


_ALIASES: dict[str, str] = _build_aliases()


def _mechanical_form(tag: str) -> str:
    """The pre-map runtime normal form: lowercase, whitespace runs → hyphens.

    This is the historical enrich-time normalization (previously
    ``_normalize_tag`` in enrich_companies), kept as the passthrough form so
    unknown tags come out exactly as they always did.
    """
    return re.sub(r"\s+", "-", tag.lower().strip())


def canonicalize_tag(tag: str) -> str:
    """Map one freeform tag onto the canonical vocabulary.

    Known aliases (case- and separator-insensitive) collapse to their
    canonical lowercase-hyphenated form. Unknown tags pass through in the
    mechanical lowercase-hyphenated form — the vocabulary stays open.
    """
    mechanical = _mechanical_form(tag)
    return _ALIASES.get(_key(mechanical), mechanical)


def canonicalize_tags(tags: Iterable[str]) -> list[str]:
    """Canonicalize a tag list: map each tag, drop blanks, dedupe.

    Order is preserved (first occurrence wins), so a list whose members
    collapse onto the same canonical tag shrinks rather than repeats. No cap
    is applied — the runtime write path has never truncated tags.
    """
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if not tag.strip():
            continue
        canon = canonicalize_tag(tag)
        if canon not in seen:
            seen.add(canon)
            result.append(canon)
    return result
