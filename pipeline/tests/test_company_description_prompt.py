"""Tests for nous.llm.prompts.company_description."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nous.llm.prompts.company_description import (
    CompanyDescription,
    PersonExtraction,
    build_prompt,
)

# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_contains_company_name() -> None:
    prompt = build_prompt(company_name="Acme", cleaned_text="we build X")
    assert "Acme" in prompt


def test_build_prompt_contains_cleaned_text() -> None:
    prompt = build_prompt(company_name="Acme", cleaned_text="we build X")
    assert "we build X" in prompt


def test_build_prompt_contains_both_fields() -> None:
    """Both interpolations appear in the same prompt string."""
    prompt = build_prompt(company_name="TestCo", cleaned_text="some scraped text here")
    assert "TestCo" in prompt
    assert "some scraped text here" in prompt


# ---------------------------------------------------------------------------
# CompanyDescription Pydantic round-trip
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {
    "description_short": "A platform for developer tooling.",
    "description_long": "Paragraph one.\n\nParagraph two.",
    "primary_category": "developer tools",
    "tags": ["api", "cloud", "saas"],
    "website_state": "ok",
}


def test_company_description_accepts_valid_json() -> None:
    raw = json.dumps(VALID_PAYLOAD)
    obj = CompanyDescription.model_validate_json(raw)
    assert obj.description_short == VALID_PAYLOAD["description_short"]
    assert obj.primary_category == VALID_PAYLOAD["primary_category"]
    assert obj.tags == VALID_PAYLOAD["tags"]


def test_company_description_tags_default_empty() -> None:
    """tags has a default of [] so it can be omitted."""
    payload = {
        "description_short": "Short.",
        "description_long": "Long.",
        "primary_category": "fintech",
        "website_state": "ok",
    }
    obj = CompanyDescription.model_validate_json(json.dumps(payload))
    assert obj.tags == []


def test_build_prompt_asks_for_people() -> None:
    """The prompt instructs the model to extract leadership/founders."""
    prompt = build_prompt(company_name="Acme", cleaned_text="we build X")
    assert "people" in prompt
    assert "CEO" in prompt


def test_company_description_people_default_empty() -> None:
    """people has a default of [] so a site that names no leaders is fine."""
    obj = CompanyDescription.model_validate_json(json.dumps(VALID_PAYLOAD))
    assert obj.people == []


def test_company_description_parses_people() -> None:
    payload = {
        **VALID_PAYLOAD,
        "people": [
            {"name": "Ada Lovelace", "role": "CEO"},
            {"name": "Alan Turing", "role": "CTO"},
        ],
    }
    obj = CompanyDescription.model_validate_json(json.dumps(payload))
    assert obj.people == [
        PersonExtraction(name="Ada Lovelace", role="CEO"),
        PersonExtraction(name="Alan Turing", role="CTO"),
    ]


def test_company_description_rejects_missing_description_short() -> None:
    """description_short is required; omitting it should raise ValidationError."""
    payload = {
        "description_long": "Long description here.",
        "primary_category": "fintech",
        "tags": [],
    }
    with pytest.raises(ValidationError):
        CompanyDescription.model_validate_json(json.dumps(payload))


def test_company_description_rejects_missing_description_long() -> None:
    """description_long is required; omitting it should raise ValidationError."""
    payload = {
        "description_short": "Short.",
        "primary_category": "fintech",
        "tags": [],
    }
    with pytest.raises(ValidationError):
        CompanyDescription.model_validate_json(json.dumps(payload))


def test_company_description_rejects_missing_primary_category() -> None:
    """primary_category is required; omitting it should raise ValidationError."""
    payload = {
        "description_short": "Short.",
        "description_long": "Long.",
        "tags": [],
    }
    with pytest.raises(ValidationError):
        CompanyDescription.model_validate_json(json.dumps(payload))


# ---------------------------------------------------------------------------
# Implausible-roster guard (H2) — testimonial/customer names mis-extracted as
# leadership tend to share one singular C-suite title across several people.
# ---------------------------------------------------------------------------


def test_shared_singular_exec_title_roster_dropped() -> None:
    """Three people all 'Co-Founder, COO' (the Shippo signature) → all dropped:
    only one COO can exist, so none of the claims is trustworthy."""
    payload = {
        **VALID_PAYLOAD,
        "people": [
            {"name": "Tiffany Jones", "role": "Co-Founder, COO"},
            {"name": "Nancey Harris", "role": "Co-Founder, COO"},
            {"name": "Wendy Webster", "role": "Co-Founder, COO"},
        ],
    }
    obj = CompanyDescription.model_validate_json(json.dumps(payload))
    assert obj.people == []


def test_distinct_exec_roles_are_kept() -> None:
    payload = {
        **VALID_PAYLOAD,
        "people": [
            {"name": "Ada Lovelace", "role": "CEO"},
            {"name": "Alan Turing", "role": "CTO"},
            {"name": "Grace Hopper", "role": "Co-Founder"},
        ],
    }
    obj = CompanyDescription.model_validate_json(json.dumps(payload))
    assert {p.name for p in obj.people} == {"Ada Lovelace", "Alan Turing", "Grace Hopper"}


def test_multiple_cofounders_without_singular_title_kept() -> None:
    """Several 'Co-Founder'/'Founder' rows are legitimate — no singular C-suite
    title is contested, so nothing is dropped."""
    payload = {
        **VALID_PAYLOAD,
        "people": [
            {"name": "A", "role": "Co-Founder"},
            {"name": "B", "role": "Co-Founder"},
            {"name": "C", "role": "Founder"},
        ],
    }
    obj = CompanyDescription.model_validate_json(json.dumps(payload))
    assert len(obj.people) == 3


def test_contested_title_drops_only_claimants() -> None:
    """A contested COO drops the COO claimants but keeps the distinct CEO."""
    payload = {
        **VALID_PAYLOAD,
        "people": [
            {"name": "Real CEO", "role": "CEO"},
            {"name": "Fake 1", "role": "COO"},
            {"name": "Fake 2", "role": "COO"},
        ],
    }
    obj = CompanyDescription.model_validate_json(json.dumps(payload))
    assert [p.name for p in obj.people] == ["Real CEO"]


def test_prompt_tells_model_to_ignore_testimonials() -> None:
    prompt = build_prompt(company_name="Acme", cleaned_text="x").lower()
    assert "testimonial" in prompt
    assert "more than one person" in prompt
