"""Unit tests for the Wikidata official-website selection core (no network).

Exercises the three precision gates (name match / org type / has-P856) against
the real name-collision cases probed live during design: "Perplexity" resolves,
the "Clay" family-name entity and a company Wikidata has no website for both
correctly yield nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from nous.sources.wikidata import (
    WikidataClient,
    WikidataFacts,
    WikidataMatch,
    _extract_entity_description,
    _extract_inception_year,
    _extract_labels,
    _extract_qid_values,
    _origin,
    _resolve_labels,
    select_entity_facts,
    select_official_website,
)


def _entity(
    *,
    label: str,
    aliases: list[str] | None = None,
    instance_of: list[str] | None = None,
    websites: list[str] | None = None,
) -> dict[str, Any]:
    claims: dict[str, Any] = {}
    if instance_of is not None:
        claims["P31"] = [
            {"mainsnak": {"datavalue": {"value": {"id": qid}}}} for qid in instance_of
        ]
    if websites is not None:
        claims["P856"] = [
            {"mainsnak": {"datavalue": {"value": url}}} for url in websites
        ]
    return {
        "labels": {"en": {"value": label}},
        "aliases": {"en": [{"value": a} for a in (aliases or [])]},
        "claims": claims,
    }


def _payload(entities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"entities": entities}


def test_resolves_prominent_company() -> None:
    entities = _payload(
        {
            "Q124333951": _entity(
                label="Perplexity AI",
                aliases=["Perplexity"],
                instance_of=["Q4830453", "Q43229"],
                websites=["https://www.perplexity.ai/hub/"],
            )
        }
    )
    match = select_official_website("Perplexity", ["Q124333951"], entities)
    assert isinstance(match, WikidataMatch)
    # P856 sub-path is canonicalized to the origin.
    assert match.website == "https://www.perplexity.ai/"
    assert match.entity_url == "https://www.wikidata.org/wiki/Q124333951"
    assert match.qid == "Q124333951"


def test_name_variant_matches_either_direction() -> None:
    """Company "Perplexity AI" matches an entity labelled just "Perplexity"."""
    entities = _payload(
        {
            "Q1": _entity(
                label="Perplexity",
                instance_of=["Q783794"],
                websites=["https://perplexity.ai"],
            )
        }
    )
    match = select_official_website("Perplexity AI", ["Q1"], entities)
    assert match is not None
    assert match.website == "https://perplexity.ai/"


def test_family_name_collision_rejected() -> None:
    """The top "Clay" hit is a family name — no org type, no P856 → no match."""
    entities = _payload(
        {
            "Q12787061": _entity(
                label="Clay",
                instance_of=["Q101352"],  # family name
                websites=None,
            )
        }
    )
    assert select_official_website("Clay", ["Q12787061"], entities) is None


def test_company_without_website_yields_nothing() -> None:
    """Wikidata knows Hebbia is a company but states no P856 → no fabrication."""
    entities = _payload(
        {"Q135708791": _entity(label="Hebbia", instance_of=["Q783794"], websites=[])}
    )
    assert select_official_website("Hebbia", ["Q135708791"], entities) is None


def test_org_type_required_even_with_website() -> None:
    """A same-named non-org that happens to carry a P856 is still rejected."""
    entities = _payload(
        {
            "Q42": _entity(
                label="Clay",
                instance_of=["Q42302"],  # 'clay' the material
                websites=["https://clay.example/"],
            )
        }
    )
    assert select_official_website("Clay", ["Q42"], entities) is None


def test_aggregator_website_rejected() -> None:
    """A P856 pointing at a social/aggregator host is never accepted."""
    entities = _payload(
        {
            "Q1": _entity(
                label="Widgets",
                instance_of=["Q4830453"],
                websites=["https://www.linkedin.com/company/widgets"],
            )
        }
    )
    assert select_official_website("Widgets", ["Q1"], entities) is None


def test_picks_first_passing_candidate_in_search_order() -> None:
    """Search-relevance order breaks ties: the concept hit is skipped, the
    company hit (second) is chosen."""
    entities = _payload(
        {
            "Q_concept": _entity(label="Notion", instance_of=["Q151885"]),  # concept
            "Q_company": _entity(
                label="Notion",
                instance_of=["Q4830453"],
                websites=["https://www.notion.so/"],
            ),
        }
    )
    match = select_official_website("Notion", ["Q_concept", "Q_company"], entities)
    assert match is not None
    assert match.website == "https://www.notion.so/"
    assert match.qid == "Q_company"


def test_missing_entity_skipped() -> None:
    entities = _payload({"Q1": {"missing": ""}})
    assert select_official_website("Whatever", ["Q1"], entities) is None


def test_no_name_match_rejected() -> None:
    entities = _payload(
        {
            "Q1": _entity(
                label="Totally Different Corp",
                instance_of=["Q4830453"],
                websites=["https://different.example/"],
            )
        }
    )
    assert select_official_website("Perplexity", ["Q1"], entities) is None


def test_country_conflict_rejected() -> None:
    """A US company must not adopt a confirmed-French same-named entity's site."""
    entities = _payload(
        {
            "Q30262164": {
                "labels": {"en": {"value": "Apex Technologies"}},
                "aliases": {"en": []},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q4830453"}}}}],
                    "P17": [{"mainsnak": {"datavalue": {"value": {"id": "Q142"}}}}],
                    "P856": [{"mainsnak": {"datavalue": {"value": "http://www.apex-t.com/"}}}],
                },
            }
        }
    )
    # Company known to be US → the French entity is rejected.
    assert (
        select_official_website(
            "Apex Technologies", ["Q30262164"], entities, company_country="US"
        )
        is None
    )
    # Unknown company country → conservative gate does not fire, still resolves.
    assert (
        select_official_website("Apex Technologies", ["Q30262164"], entities)
        is not None
    )
    # Matching country → resolves.
    assert (
        select_official_website(
            "Apex Technologies", ["Q30262164"], entities, company_country="FR"
        )
        is not None
    )


def test_country_gate_noop_when_entity_country_unmapped() -> None:
    """An entity whose P17 we can't map is treated as no country signal."""
    entities = _payload(
        {
            "Q1": {
                "labels": {"en": {"value": "Widgets"}},
                "aliases": {"en": []},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q4830453"}}}}],
                    "P17": [{"mainsnak": {"datavalue": {"value": {"id": "Q99999999"}}}}],
                    "P856": [{"mainsnak": {"datavalue": {"value": "https://widgets.example/"}}}],
                },
            }
        }
    )
    assert (
        select_official_website("Widgets", ["Q1"], entities, company_country="US")
        is not None
    )


def test_origin_canonicalization() -> None:
    assert _origin("https://www.perplexity.ai/hub/") == "https://www.perplexity.ai/"
    assert _origin("https://mistral.ai/fr") == "https://mistral.ai/"
    assert _origin("https://example.com") == "https://example.com/"
    assert _origin("ftp://example.com/x") is None
    assert _origin("not a url") is None


# ── entity FACTS extractors + selection (describe-fallback source) ───────────


def _facts_entity(
    *,
    label: str,
    aliases: list[str] | None = None,
    instance_of: list[str] | None = None,
    description: str | None = None,
    inception: str | None = None,
    hq_qids: list[str] | None = None,
    industry_qids: list[str] | None = None,
    founder_qids: list[str] | None = None,
    websites: list[str] | None = None,
) -> dict[str, Any]:
    entity = _entity(
        label=label, aliases=aliases, instance_of=instance_of, websites=websites
    )
    if description is not None:
        entity["descriptions"] = {"en": {"value": description}}
    claims = entity["claims"]
    if inception is not None:
        claims["P571"] = [{"mainsnak": {"datavalue": {"value": {"time": inception}}}}]

    def _qid_stmts(qids: list[str]) -> list[dict[str, Any]]:
        return [
            {"mainsnak": {"datavalue": {"value": {"id": q}}}} for q in qids
        ]

    if hq_qids is not None:
        claims["P159"] = _qid_stmts(hq_qids)
    if industry_qids is not None:
        claims["P452"] = _qid_stmts(industry_qids)
    if founder_qids is not None:
        claims["P112"] = _qid_stmts(founder_qids)
    return entity


def test_extract_entity_description() -> None:
    entity = _facts_entity(label="SpaceX", description="American aerospace manufacturer")
    assert _extract_entity_description(entity) == "American aerospace manufacturer"
    # Absent / blank → None.
    assert _extract_entity_description(_facts_entity(label="X")) is None
    blank = _facts_entity(label="X", description="   ")
    assert _extract_entity_description(blank) is None


def test_extract_inception_year() -> None:
    claims = _facts_entity(label="X", inception="+2015-03-14T00:00:00Z")["claims"]
    assert _extract_inception_year(claims) == 2015
    # No P571 → None.
    assert _extract_inception_year({}) is None
    # Multiple statements → the earliest year wins.
    claims_multi = {
        "P571": [
            {"mainsnak": {"datavalue": {"value": {"time": "+2020-00-00T00:00:00Z"}}}},
            {"mainsnak": {"datavalue": {"value": {"time": "+2012-00-00T00:00:00Z"}}}},
        ]
    }
    assert _extract_inception_year(claims_multi) == 2012


def test_extract_qid_values_orders_and_dedupes() -> None:
    claims = _facts_entity(
        label="X", industry_qids=["Q1", "Q2", "Q1"]
    )["claims"]
    assert _extract_qid_values(claims, "P452") == ["Q1", "Q2"]
    assert _extract_qid_values(claims, "P999") == []


def test_extract_labels_and_resolve() -> None:
    payload = {
        "entities": {
            "Q1": {"labels": {"en": {"value": "Aerospace"}}},
            "Q2": {"missing": ""},
            "Q3": {"labels": {"en": {"value": "Hawthorne"}}},
        }
    }
    labels = _extract_labels(payload)
    assert labels == {"Q1": "Aerospace", "Q3": "Hawthorne"}
    # Unresolved QIDs are dropped (a bare QID is useless as evidence).
    assert _resolve_labels(["Q1", "Q2", "Q3"], labels) == ["Aerospace", "Hawthorne"]


def test_select_entity_facts_returns_qid_valued_facts() -> None:
    entities = _payload(
        {
            "Q1": _facts_entity(
                label="SpaceX",
                instance_of=["Q4830453"],
                description="American aerospace manufacturer",
                inception="+2002-00-00T00:00:00Z",
                hq_qids=["Q49255"],
                industry_qids=["Q7411", "Q7411"],
                founder_qids=["Q317521"],
                websites=["https://www.spacex.com/hub/"],
            )
        }
    )
    facts = select_entity_facts("SpaceX", ["Q1"], entities)
    assert isinstance(facts, WikidataFacts)
    assert facts.qid == "Q1"
    assert facts.entity_url == "https://www.wikidata.org/wiki/Q1"
    assert facts.entity_description == "American aerospace manufacturer"
    assert facts.inception_year == 2002
    # QID-valued facts are RAW QIDs here — the async caller resolves labels.
    assert facts.hq == ["Q49255"]
    assert facts.industries == ["Q7411"]
    assert facts.founders == ["Q317521"]
    # Website reuses the origin canonicalization (P856 present).
    assert facts.website == "https://www.spacex.com/"


def test_select_entity_facts_needs_no_website() -> None:
    """A company Wikidata knows but has no P856 for still yields facts (unlike
    select_official_website, which requires a website)."""
    entities = _payload(
        {
            "Q1": _facts_entity(
                label="Hebbia",
                instance_of=["Q783794"],
                description="American software company",
                websites=[],
            )
        }
    )
    facts = select_entity_facts("Hebbia", ["Q1"], entities)
    assert facts is not None
    assert facts.website is None
    assert facts.entity_description == "American software company"
    # The same name collision the website lookup rejects is still rejected here.
    assert select_official_website("Hebbia", ["Q1"], entities) is None


def test_select_entity_facts_applies_gates() -> None:
    # A same-named non-org (no org type) is rejected by the shared gate.
    entities = _payload(
        {"Q1": _facts_entity(label="Clay", instance_of=["Q101352"])}
    )
    assert select_entity_facts("Clay", ["Q1"], entities) is None
    # Country conflict is rejected too (shared gate 3).
    fr = _payload(
        {
            "Q2": {
                "labels": {"en": {"value": "Apex"}},
                "aliases": {"en": []},
                "descriptions": {"en": {"value": "French company"}},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q4830453"}}}}],
                    "P17": [{"mainsnak": {"datavalue": {"value": {"id": "Q142"}}}}],
                },
            }
        }
    )
    assert select_entity_facts("Apex", ["Q2"], fr, company_country="US") is None
    assert select_entity_facts("Apex", ["Q2"], fr, company_country="FR") is not None


async def test_entity_facts_batch_resolves_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """entity_facts issues ONE extra labels call and maps hq/industry/founder
    QIDs to English labels (no network — the private calls are stubbed)."""
    client = WikidataClient("nous-test (test@example.com)")

    entity_payload = _payload(
        {
            "Q1": _facts_entity(
                label="SpaceX",
                instance_of=["Q4830453"],
                description="American aerospace manufacturer",
                hq_qids=["Q49255"],
                industry_qids=["Q7411"],
                founder_qids=["Q317521"],
                websites=["https://www.spacex.com/"],
            )
        }
    )
    labels_payload = {
        "entities": {
            "Q49255": {"labels": {"en": {"value": "Hawthorne, California"}}},
            "Q7411": {"labels": {"en": {"value": "aerospace"}}},
            "Q317521": {"labels": {"en": {"value": "Elon Musk"}}},
        }
    }

    async def fake_search(name: str, limit: int) -> list[str]:
        return ["Q1"]

    async def fake_get_entities(ids: list[str]) -> dict[str, Any]:
        return entity_payload

    async def fake_get_labels(ids: list[str]) -> dict[str, Any]:
        assert set(ids) == {"Q49255", "Q7411", "Q317521"}
        return labels_payload

    monkeypatch.setattr(client, "_search", fake_search)
    monkeypatch.setattr(client, "_get_entities", fake_get_entities)
    monkeypatch.setattr(client, "_get_labels", fake_get_labels)

    facts = await client.entity_facts("SpaceX")
    assert facts is not None
    assert facts.hq == ["Hawthorne, California"]
    assert facts.industries == ["aerospace"]
    assert facts.founders == ["Elon Musk"]
    assert facts.entity_description == "American aerospace manufacturer"


async def test_entity_facts_no_match_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = WikidataClient("nous-test (test@example.com)")

    async def fake_search(name: str, limit: int) -> list[str]:
        return []

    monkeypatch.setattr(client, "_search", fake_search)
    assert await client.entity_facts("Nonexistent") is None
