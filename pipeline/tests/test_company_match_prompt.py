"""Tests for nous.llm.prompts.company_match.

Pure prompt-building + Pydantic schema validation. No LLM call.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nous.llm.prompts.company_match import (
    CompanyMatch,
    build_company_match_prompt,
)

# ---------------------------------------------------------------------------
# build_company_match_prompt
# ---------------------------------------------------------------------------


COMPANY_A: dict[str, object] = {
    "name": "Acme AI",
    "website": "https://acme.ai",
    "description": "AI agents for sales teams.",
    "hq_city": "San Francisco",
    "hq_state": "CA",
}
COMPANY_B: dict[str, object] = {
    "name": "Acme AI, Inc.",
    "website": "https://www.acme.ai",
    "description": "Sales automation powered by AI agents.",
    "hq_city": "San Francisco",
    "hq_state": "CA",
}


def test_build_prompt_contains_both_company_fields() -> None:
    prompt = build_company_match_prompt(COMPANY_A, COMPANY_B)
    assert "Company A:" in prompt
    assert "Company B:" in prompt
    assert "Acme AI" in prompt
    assert "Acme AI, Inc." in prompt
    assert "https://acme.ai" in prompt
    assert "AI agents for sales teams." in prompt
    assert "San Francisco, CA" in prompt


def test_build_prompt_renders_missing_fields_as_unknown() -> None:
    """Absent / blank fields are shown as ``(unknown)`` rather than blank lines."""
    sparse: dict[str, object] = {"name": "Mystery Co"}
    prompt = build_company_match_prompt(sparse, COMPANY_B)
    assert "Mystery Co" in prompt
    # Website / description / HQ for the sparse company should be (unknown).
    assert "(unknown)" in prompt


def test_build_prompt_handles_none_values() -> None:
    """Explicit None values don't crash and render as (unknown)."""
    a: dict[str, object] = {
        "name": "Solo Co",
        "website": None,
        "description": None,
        "hq_city": None,
        "hq_state": None,
    }
    prompt = build_company_match_prompt(a, COMPANY_B)
    assert "Solo Co" in prompt
    assert "(unknown)" in prompt


def test_build_prompt_renders_partial_location() -> None:
    """A city without a state still renders the city (no trailing comma)."""
    a: dict[str, object] = {"name": "City Only Co", "hq_city": "Austin"}
    prompt = build_company_match_prompt(a, COMPANY_B)
    assert "HQ: Austin" in prompt


def test_build_prompt_includes_conservative_rule() -> None:
    """Guard against an edit dropping the 'do not guess' instruction."""
    prompt = build_company_match_prompt(COMPANY_A, COMPANY_B)
    assert "same_company=true ONLY" in prompt
    assert "Never guess." in prompt


# ---------------------------------------------------------------------------
# CompanyMatch Pydantic round-trip
# ---------------------------------------------------------------------------


def test_company_match_accepts_high_confidence_match() -> None:
    obj = CompanyMatch.model_validate_json(
        json.dumps({"same_company": True, "confidence": "high"})
    )
    assert obj.same_company is True
    assert obj.confidence == "high"


def test_company_match_accepts_negative_low_confidence() -> None:
    obj = CompanyMatch.model_validate_json(
        json.dumps({"same_company": False, "confidence": "low"})
    )
    assert obj.same_company is False
    assert obj.confidence == "low"


def test_company_match_rejects_invalid_confidence() -> None:
    """confidence is a Literal['low','medium','high']; other values are rejected."""
    with pytest.raises(ValidationError):
        CompanyMatch.model_validate_json(
            json.dumps({"same_company": True, "confidence": "very high"})
        )


def test_company_match_rejects_missing_fields() -> None:
    with pytest.raises(ValidationError):
        CompanyMatch.model_validate_json(json.dumps({"same_company": True}))
