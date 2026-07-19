"""Wikidata "official website" lookup — a non-origin website re-mining source.

Resolves a company's homepage from Wikidata's *official website* property
(``P856``), matched by entity name **and** organization type. This routes around
the Cloudflare-403 that blocks the origin-homepage scrape (ROADMAP "route
around, don't evade"): the website-less husk cohort is *prominent* companies,
which are exactly who Wikidata indexes, and the Wikidata Action API is free and
un-Cloudflared. The Wikidata entity page is recorded as the provenance source.

Precision model — three gates, all required, so a name collision self-rejects
rather than producing a wrong site:

1. **Name match** — the company's normalized token set must be a subset of (or
   superset of) the candidate entity's label/alias tokens. Handles
   "Perplexity" ↔ "Perplexity AI" while rejecting unrelated entities.
2. **Organization type** — ``P31`` (instance-of) must intersect
   :data:`ORG_TYPE_QIDS`. Rejects the same-named person / place / material /
   given-name entities (e.g. "Clay" the family name).
3. **Has P856** — the entity must actually state an official website. Entities
   Wikidata knows are companies but has no website for (e.g. "Hebbia") correctly
   yield nothing rather than a fabricated URL.

The robots gate: ``www.wikidata.org/robots.txt`` disallows ``/w/`` for ``*``,
which covers the JSON API at ``/w/api.php``. That ``Disallow`` targets crawlers
hammering the raw wiki surface, not the Action API — Wikimedia publishes the API
for exactly this programmatic use and governs it via API:Etiquette (a good
User-Agent + reasonable rate), both of which we honor. So the API endpoint is
treated as robots-exempt, narrowly, mirroring the Google-News-RSS exemption in
``sources/news.py``. The contact-email User-Agent and 1 req/sec throttle still
apply to every call.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from nous.sources._http import DomainThrottle, ThrottledHTTPClient
from nous.sources.reject_hosts import is_aggregator_url
from nous.util.slugify import name_tokens, names_token_subset
from nous.util.ssrf import BlockedAddressError, guarded_async_client
from nous.util.url import is_storable_website

logger = logging.getLogger(__name__)

_API_URL = "https://www.wikidata.org/w/api.php"
_ENTITY_BASE = "https://www.wikidata.org/wiki/"

# Wikidata country (P17) QID → ISO-3166 alpha-2, for the country cross-check.
# Only common countries need mapping: the check is conservative (it fires only
# when BOTH the company's hq_country and the entity's mapped country are known),
# so an unmapped country QID simply means "no country signal" — never a wrong
# rejection. hq_country is stored as alpha-2 (enrich/judge/infer stages).
_COUNTRY_QID_TO_ISO2: dict[str, str] = {
    "Q30": "US",  # United States
    "Q145": "GB",  # United Kingdom
    "Q183": "DE",  # Germany
    "Q142": "FR",  # France
    "Q16": "CA",  # Canada
    "Q801": "IL",  # Israel
    "Q668": "IN",  # India
    "Q55": "NL",  # Netherlands
    "Q34": "SE",  # Sweden
    "Q39": "CH",  # Switzerland
    "Q408": "AU",  # Australia
    "Q334": "SG",  # Singapore
    "Q27": "IE",  # Ireland
    "Q29": "ES",  # Spain
    "Q38": "IT",  # Italy
    "Q33": "FI",  # Finland
    "Q35": "DK",  # Denmark
    "Q20": "NO",  # Norway
    "Q17": "JP",  # Japan
    "Q884": "KR",  # South Korea
    "Q155": "BR",  # Brazil
    "Q159": "RU",  # Russia
    "Q148": "CN",  # People's Republic of China
    "Q212": "UA",  # Ukraine
    "Q40": "AT",  # Austria
    "Q31": "BE",  # Belgium
    "Q45": "PT",  # Portugal
    "Q233": "MT",  # Malta
    "Q191": "EE",  # Estonia
}

# P31 (instance-of) QIDs that mark an entity as a company / organization. Curated
# and generous: the name-match + P856 gates do the real precision work, so this
# only needs to admit the common company subtypes and reject non-orgs (people,
# places, materials, given/family names). Extend on a confirmed miss.
ORG_TYPE_QIDS: frozenset[str] = frozenset(
    {
        "Q4830453",  # business
        "Q783794",  # company
        "Q43229",  # organization
        "Q6881511",  # enterprise
        "Q18388277",  # technology company
        "Q1058914",  # software company
        "Q891723",  # public company
        "Q167037",  # corporation
        "Q161726",  # multinational corporation
        "Q219577",  # holding company
        "Q133284914",  # artificial intelligence website
        "Q66625719",  # startup company
        "Q1785271",  # subsidiary
        "Q2225339",  # brand (some product companies file here)
    }
)


# Cap on how many QID-valued facts (hq / industry / founder) get their English
# labels batch-resolved in the one extra wbgetentities call. A prominent company
# rarely states more than a handful; a cap bounds the single request's size.
MAX_LABEL_QIDS: int = 10


class WikidataMatch(BaseModel):
    """A confirmed Wikidata entity match carrying an official website."""

    qid: str
    entity_url: str  # https://www.wikidata.org/wiki/Q… — the provenance source
    website: str  # origin-canonicalized P856 official website
    matched_label: str


class WikidataFacts(BaseModel):
    """Entity FACTS for the same name+org-type matched entity as ``WikidataMatch``.

    The describe-fallback evidence source: the third-party facts nous can cite
    for a company whose own site it cannot read. ``entity_description`` (Wikidata's
    one-line "American aerospace manufacturer") is the single highest-value fact —
    it is exactly the non-funding descriptor the describe-fallback prompt gates on.
    QID-valued facts (``hq`` / ``industries`` / ``founders``) are stored as their
    resolved English LABELS, not QIDs, so they read as evidence. ``website`` mirrors
    ``WikidataMatch.website`` (or None when the entity states no usable P856); every
    fact is attributable to ``entity_url``.
    """

    qid: str
    entity_url: str  # https://www.wikidata.org/wiki/Q… — the provenance source
    matched_label: str
    entity_description: str | None = None
    inception_year: int | None = None
    hq: list[str] = Field(default_factory=list)  # headquarters labels (P159)
    industries: list[str] = Field(default_factory=list)  # industry labels (P452)
    founders: list[str] = Field(default_factory=list)  # founder labels (P112)
    website: str | None = None  # origin-canonicalized P856, when stated


def _names_match(company_name: str, candidate_names: list[str]) -> str | None:
    """Return the first candidate label whose tokens subset-match the company."""
    if not name_tokens(company_name):
        return None
    for cand in candidate_names:
        if names_token_subset(company_name, cand):
            return cand
    return None


def _origin(url: str) -> str | None:
    """Canonicalize a P856 URL to its scheme+host origin (drop path/query).

    Wikidata often stores a sub-path ("…/hub/", "…/fr"); the ``website`` field
    means the homepage, so we keep only the origin.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}/"


def _extract_instance_of(claims: dict[str, Any]) -> set[str]:
    """QIDs from all P31 (instance-of) statements."""
    out: set[str] = set()
    for stmt in claims.get("P31", []):
        try:
            out.add(stmt["mainsnak"]["datavalue"]["value"]["id"])
        except (KeyError, TypeError):
            continue
    return out


def _extract_countries(claims: dict[str, Any]) -> set[str]:
    """ISO-3166 alpha-2 codes from P17 (country) statements we can map.

    Unmapped country QIDs are dropped (treated as no signal), so the cross-check
    never rejects on a country it doesn't recognize.
    """
    out: set[str] = set()
    for stmt in claims.get("P17", []):
        try:
            qid = stmt["mainsnak"]["datavalue"]["value"]["id"]
        except (KeyError, TypeError):
            continue
        iso = _COUNTRY_QID_TO_ISO2.get(qid)
        if iso is not None:
            out.add(iso)
    return out


def _extract_official_websites(claims: dict[str, Any]) -> list[str]:
    """Raw P856 (official website) string values, in statement order."""
    out: list[str] = []
    for stmt in claims.get("P856", []):
        try:
            value = stmt["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue
        if isinstance(value, str):
            out.append(value)
    return out


def _extract_entity_description(entity: dict[str, Any]) -> str | None:
    """The English one-line entity description (e.g. "American aerospace
    manufacturer"), or None. This is the single highest-value describe-fallback
    fact — a ready-made non-funding descriptor curated by Wikidata."""
    value = entity.get("descriptions", {}).get("en", {}).get("value")
    return value if isinstance(value, str) and value.strip() else None


def _extract_inception_year(claims: dict[str, Any]) -> int | None:
    """Founding year from the earliest P571 (inception) time statement, or None.

    Wikidata times are ISO-ish strings with a leading sign ("+2015-00-00T00:00:00Z");
    only the year is reliable (month/day are often 0), so we take just the year.
    """
    years: list[int] = []
    for stmt in claims.get("P571", []):
        try:
            time_str = stmt["mainsnak"]["datavalue"]["value"]["time"]
        except (KeyError, TypeError):
            continue
        match = re.match(r"[+-]?(\d{1,4})", str(time_str))
        if match:
            year = int(match.group(1))
            if year > 0:
                years.append(year)
    return min(years) if years else None


def _extract_qid_values(claims: dict[str, Any], prop: str) -> list[str]:
    """Ordered, de-duplicated QID values of a wikibase-item property (P159 /
    P452 / P112). The QIDs are label-resolved by the async caller."""
    out: list[str] = []
    for stmt in claims.get(prop, []):
        try:
            qid = stmt["mainsnak"]["datavalue"]["value"]["id"]
        except (KeyError, TypeError):
            continue
        if qid not in out:
            out.append(qid)
    return out


def _extract_labels(entities: dict[str, Any]) -> dict[str, str]:
    """QID → English label map from a ``props=labels`` wbgetentities payload."""
    out: dict[str, str] = {}
    for qid, entity in entities.get("entities", {}).items():
        if not isinstance(entity, dict) or "missing" in entity:
            continue
        label = entity.get("labels", {}).get("en", {}).get("value")
        if isinstance(label, str) and label.strip():
            out[qid] = label
    return out


def _resolve_labels(qids: list[str], labels: dict[str, str]) -> list[str]:
    """Map QIDs to labels, dropping any that did not resolve (a bare QID is
    useless as human-readable evidence)."""
    return [labels[q] for q in qids if q in labels]


def _entity_names(entity: dict[str, Any]) -> list[str]:
    """English label + all English aliases for an entity."""
    names: list[str] = []
    label = entity.get("labels", {}).get("en", {}).get("value")
    if label:
        names.append(label)
    for alias in entity.get("aliases", {}).get("en", []):
        value = alias.get("value")
        if value:
            names.append(value)
    return names


def _entity_matches(
    company_name: str,
    entity: dict[str, Any],
    *,
    want_country: str | None,
) -> str | None:
    """Gates 1-3 shared by both public lookups — name / org-type / country.

    Returns the matched label when the entity is a name + org-type match that
    does not conflict on country, else None. The final, lookup-specific gate (a
    usable P856 for ``select_official_website``; nothing extra for
    ``select_entity_facts``) is applied by each caller. Factored so both lookups
    self-reject the same name collisions identically; the caller-specific gate is
    what keeps their behavior distinct.
    """
    # Gate 1: name match against label + aliases.
    matched_label = _names_match(company_name, _entity_names(entity))
    if matched_label is None:
        return None
    claims = entity.get("claims", {})
    # Gate 2: organization type.
    if not (_extract_instance_of(claims) & ORG_TYPE_QIDS):
        return None
    # Gate 3: country cross-check (conservative — only on a known conflict).
    if want_country is not None:
        entity_countries = _extract_countries(claims)
        if entity_countries and want_country not in entity_countries:
            return None
    return matched_label


def select_official_website(
    company_name: str,
    search_ids: list[str],
    entities: dict[str, Any],
    *,
    company_country: str | None = None,
) -> WikidataMatch | None:
    """Pure selection core (no I/O): pick the best P856 match, or None.

    ``search_ids`` are candidate QIDs in Wikidata search-relevance order;
    ``entities`` is the ``wbgetentities`` ``{"entities": {...}}`` payload. The
    first candidate passing all gates (name / org-type / country / P856) wins,
    so search relevance breaks ties. ``company_country`` is the company's stored
    ISO-3166 alpha-2 ``hq_country`` (or None); the country gate is conservative —
    it rejects a candidate only when both that and the entity's mapped P17
    country are known and conflict, so a US-focused directory never adopts a
    confirmed-foreign same-named company's site (the Apex-France case), while a
    NULL-country husk still resolves.
    """
    want_country = (company_country or "").strip().upper() or None
    ent_map = entities.get("entities", {})
    for qid in search_ids:
        entity = ent_map.get(qid)
        # wbgetentities marks an absent id with a "missing" key ({"missing": ""}).
        if not isinstance(entity, dict) or "missing" in entity:
            continue
        claims = entity.get("claims", {})
        if not isinstance(claims, dict):
            continue

        # Gates 1-3: name / org-type / country (shared with select_entity_facts).
        matched_label = _entity_matches(
            company_name, entity, want_country=want_country
        )
        if matched_label is None:
            continue

        # Gate 4: a usable official website.
        website = _first_usable_website(claims)
        if website is None:
            continue

        return WikidataMatch(
            qid=qid,
            entity_url=f"{_ENTITY_BASE}{qid}",
            website=website,
            matched_label=matched_label,
        )
    return None


def _first_usable_website(claims: dict[str, Any]) -> str | None:
    """First P856 value that canonicalizes to a storable, non-aggregator origin."""
    for raw in _extract_official_websites(claims):
        origin = _origin(raw)
        if origin is None or not is_storable_website(origin):
            continue
        if is_aggregator_url(origin):  # never accept a directory/social host
            continue
        return origin
    return None


def select_entity_facts(
    company_name: str,
    search_ids: list[str],
    entities: dict[str, Any],
    *,
    company_country: str | None = None,
) -> WikidataFacts | None:
    """Pure selection core (no I/O): facts for the first name+org-type matched
    entity, or None.

    Same gates 1-3 as :func:`select_official_website` (so the two lookups agree
    on which entity is this company), but WITHOUT the P856 requirement — an entity
    Wikidata knows is this company yet states no website can still carry a
    description / inception / industry, which is exactly the describe-fallback
    evidence. The QID-valued facts (``hq`` / ``industries`` / ``founders``) come
    back as RAW QIDs here; the async caller batch-resolves them to English labels.
    """
    want_country = (company_country or "").strip().upper() or None
    ent_map = entities.get("entities", {})
    for qid in search_ids:
        entity = ent_map.get(qid)
        # wbgetentities marks an absent id with a "missing" key ({"missing": ""}).
        if not isinstance(entity, dict) or "missing" in entity:
            continue
        claims = entity.get("claims", {})
        if not isinstance(claims, dict):
            continue

        matched_label = _entity_matches(
            company_name, entity, want_country=want_country
        )
        if matched_label is None:
            continue

        return WikidataFacts(
            qid=qid,
            entity_url=f"{_ENTITY_BASE}{qid}",
            matched_label=matched_label,
            entity_description=_extract_entity_description(entity),
            inception_year=_extract_inception_year(claims),
            hq=_extract_qid_values(claims, "P159"),
            industries=_extract_qid_values(claims, "P452"),
            founders=_extract_qid_values(claims, "P112"),
            website=_first_usable_website(claims),
        )
    return None


class WikidataClient:
    """Async client for the Wikidata Action API (search → official website).

    Use as an async context manager. All requests carry the contact-email
    User-Agent and are throttled to 1 req/sec/domain via the shared registry.
    """

    def __init__(
        self,
        user_agent: str,
        requests_per_second_per_domain: float = 1.0,
        throttle: DomainThrottle | None = None,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise ValueError(
                "user_agent must be a non-empty string containing a contact email."
            )
        self._user_agent = user_agent
        self._http = ThrottledHTTPClient(
            requests_per_second_per_domain=requests_per_second_per_domain,
            throttle=throttle,
        )
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> WikidataClient:
        self._client = guarded_async_client(
            headers={"User-Agent": self._user_agent},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _assert_open(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("WikidataClient must be used as an async context manager.")
        return self._client

    async def _get_json(self, params: dict[str, str]) -> dict[str, Any]:
        client = self._assert_open()
        # Build the query string via httpx so values are escaped once. The
        # Action API endpoint is robots-exempt (see module docstring) — a
        # deliberate, narrow exception; throttle + User-Agent still apply.
        request = client.build_request("GET", _API_URL, params=params)
        resp = await self._http.get(client, str(request.url))
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Wikidata API returned a non-object payload")
        return data

    async def _search(self, company_name: str, limit: int) -> list[str]:
        data = await self._get_json(
            {
                "action": "wbsearchentities",
                "search": company_name,
                "language": "en",
                "uselang": "en",
                "type": "item",
                "limit": str(limit),
                "format": "json",
            }
        )
        hits = data.get("search", [])
        return [h["id"] for h in hits if isinstance(h, dict) and h.get("id")]

    async def _get_entities(self, ids: list[str]) -> dict[str, Any]:
        return await self._get_json(
            {
                "action": "wbgetentities",
                "ids": "|".join(ids),
                # "descriptions" carries Wikidata's one-line entity summary
                # ("American aerospace manufacturer") — the highest-value
                # describe-fallback fact; harmless to official_website.
                "props": "labels|aliases|claims|descriptions",
                "languages": "en",
                "format": "json",
            }
        )

    async def _get_labels(self, ids: list[str]) -> dict[str, Any]:
        """Labels-only wbgetentities for QID-valued facts (hq/industry/founder)."""
        return await self._get_json(
            {
                "action": "wbgetentities",
                "ids": "|".join(ids),
                "props": "labels",
                "languages": "en",
                "format": "json",
            }
        )

    async def official_website(
        self,
        company_name: str,
        *,
        company_country: str | None = None,
        limit: int = 5,
    ) -> WikidataMatch | None:
        """Resolve ``company_name`` to a confirmed official website, or None.

        Two API calls: search then get-entities. ``company_country`` (the
        company's ISO-3166 alpha-2 ``hq_country``) drives the conservative
        country cross-check. Returns None on no match or on any transport/parse
        failure (the caller treats it as "this source had nothing"), never
        raising for an ordinary miss.
        """
        try:
            ids = await self._search(company_name, limit)
            if not ids:
                return None
            entities = await self._get_entities(ids)
        except (httpx.HTTPStatusError, httpx.RequestError, BlockedAddressError) as exc:
            logger.info("wikidata lookup failed for %r: %s", company_name, exc)
            return None
        except (ValueError, KeyError, TypeError) as exc:
            logger.info("wikidata payload parse failed for %r: %s", company_name, exc)
            return None
        return select_official_website(
            company_name, ids, entities, company_country=company_country
        )

    async def entity_facts(
        self,
        company_name: str,
        *,
        company_country: str | None = None,
        limit: int = 5,
    ) -> WikidataFacts | None:
        """Resolve ``company_name`` to its Wikidata entity FACTS, or None.

        Reuses the exact search + org-type gate + name-match + country cross-check
        as :meth:`official_website` (same entity selection), then adds ONE extra
        ``props=labels`` call to resolve the QID-valued facts (headquarters /
        industry / founders, capped at :data:`MAX_LABEL_QIDS`) to English labels.
        So a hit costs three API calls (search, get-entities, get-labels), or two
        when the entity states no QID facts. Returns None on no match or on any
        transport/parse failure — never raises for an ordinary miss.
        """
        try:
            ids = await self._search(company_name, limit)
            if not ids:
                return None
            entities = await self._get_entities(ids)
        except (httpx.HTTPStatusError, httpx.RequestError, BlockedAddressError) as exc:
            logger.info("wikidata facts lookup failed for %r: %s", company_name, exc)
            return None
        except (ValueError, KeyError, TypeError) as exc:
            logger.info("wikidata facts parse failed for %r: %s", company_name, exc)
            return None

        facts = select_entity_facts(
            company_name, ids, entities, company_country=company_country
        )
        if facts is None:
            return None

        # Batch-resolve the QID-valued facts to English labels in ONE call.
        qids = list(dict.fromkeys([*facts.hq, *facts.industries, *facts.founders]))[
            :MAX_LABEL_QIDS
        ]
        labels: dict[str, str] = {}
        if qids:
            try:
                labels = _extract_labels(await self._get_labels(qids))
            except (
                httpx.HTTPStatusError,
                httpx.RequestError,
                BlockedAddressError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                # A label-resolution miss is not fatal: the description /
                # inception facts still stand; the QID facts just drop out.
                logger.info(
                    "wikidata label resolution failed for %r: %s", company_name, exc
                )
        facts.hq = _resolve_labels(facts.hq, labels)
        facts.industries = _resolve_labels(facts.industries, labels)
        facts.founders = _resolve_labels(facts.founders, labels)
        return facts
