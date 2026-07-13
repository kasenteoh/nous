"""Unit tests for the Wikidata official-website selection core (no network).

Exercises the three precision gates (name match / org type / has-P856) against
the real name-collision cases probed live during design: "Perplexity" resolves,
the "Clay" family-name entity and a company Wikidata has no website for both
correctly yield nothing.
"""

from __future__ import annotations

from typing import Any

from nous.sources.wikidata import (
    WikidataMatch,
    _origin,
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
