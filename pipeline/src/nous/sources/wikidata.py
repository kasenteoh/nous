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
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from nous.sources._http import DomainThrottle, ThrottledHTTPClient
from nous.sources.reject_hosts import is_aggregator_url
from nous.util.slugify import name_tokens, names_token_subset
from nous.util.ssrf import BlockedAddressError, guarded_async_client
from nous.util.url import is_storable_website

logger = logging.getLogger(__name__)

_API_URL = "https://www.wikidata.org/w/api.php"
_ENTITY_BASE = "https://www.wikidata.org/wiki/"

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


class WikidataMatch(BaseModel):
    """A confirmed Wikidata entity match carrying an official website."""

    qid: str
    entity_url: str  # https://www.wikidata.org/wiki/Q… — the provenance source
    website: str  # origin-canonicalized P856 official website
    matched_label: str


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


def select_official_website(
    company_name: str,
    search_ids: list[str],
    entities: dict[str, Any],
) -> WikidataMatch | None:
    """Pure selection core (no I/O): pick the best P856 match, or None.

    ``search_ids`` are candidate QIDs in Wikidata search-relevance order;
    ``entities`` is the ``wbgetentities`` ``{"entities": {...}}`` payload. The
    first candidate passing all three gates (name / org-type / P856) wins, so
    search relevance breaks ties.
    """
    ent_map = entities.get("entities", {})
    for qid in search_ids:
        entity = ent_map.get(qid)
        # wbgetentities marks an absent id with a "missing" key ({"missing": ""}).
        if not isinstance(entity, dict) or "missing" in entity:
            continue
        claims = entity.get("claims", {})
        if not isinstance(claims, dict):
            continue

        # Gate 1: name match against label + aliases.
        matched_label = _names_match(company_name, _entity_names(entity))
        if matched_label is None:
            continue

        # Gate 2: organization type.
        if not (_extract_instance_of(claims) & ORG_TYPE_QIDS):
            continue

        # Gate 3: a usable official website.
        website: str | None = None
        for raw in _extract_official_websites(claims):
            origin = _origin(raw)
            if origin is None or not is_storable_website(origin):
                continue
            if is_aggregator_url(origin):  # never accept a directory/social host
                continue
            website = origin
            break
        if website is None:
            continue

        return WikidataMatch(
            qid=qid,
            entity_url=f"{_ENTITY_BASE}{qid}",
            website=website,
            matched_label=matched_label,
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
                "props": "labels|aliases|claims",
                "languages": "en",
                "format": "json",
            }
        )

    async def official_website(
        self, company_name: str, *, limit: int = 5
    ) -> WikidataMatch | None:
        """Resolve ``company_name`` to a confirmed official website, or None.

        Two API calls: search then get-entities. Returns None on no match or on
        any transport/parse failure (the caller treats it as "this source had
        nothing"), never raising for an ordinary miss.
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
        return select_official_website(company_name, ids, entities)
