"""Tests for nous.llm.prompts.company_eligibility.

Pure prompt-construction + Pydantic round-trip tests; no DB required (the
``judge-eligibility`` stage integration lives in ``test_judge_eligibility.py``).
"""

from __future__ import annotations

import json

from nous.llm.prompts.company_eligibility import EligibilityJudgment, build_prompt

# ---------------------------------------------------------------------------
# build_prompt — interpolation
# ---------------------------------------------------------------------------


def test_build_prompt_contains_all_inputs() -> None:
    prompt = build_prompt(
        company_name="Acme",
        description="we build X",
        cleaned_text="some scraped homepage text",
    )
    assert "Acme" in prompt
    assert "we build X" in prompt
    assert "some scraped homepage text" in prompt


# ---------------------------------------------------------------------------
# Tightened non-startup rejection guidance (the Manta / Lucra leak fix).
# Assert the prompt names each rejection category and the precision guard, so a
# future loosening of the prompt trips a test rather than silently regressing.
# ---------------------------------------------------------------------------


def test_prompt_requires_software_product() -> None:
    """A real startup must build a software product/platform and be venture-scale."""
    prompt = build_prompt(
        company_name="Acme", description="d", cleaned_text="t"
    ).lower()
    assert "software" in prompt
    assert "venture-scale" in prompt
    assert "product or platform" in prompt


def test_prompt_rejects_directories_and_listings() -> None:
    prompt = build_prompt(
        company_name="Acme", description="d", cleaned_text="t"
    ).lower()
    assert "directory" in prompt
    assert "listings site" in prompt


def test_prompt_rejects_coaching_and_info_products() -> None:
    prompt = build_prompt(
        company_name="Acme", description="d", cleaned_text="t"
    ).lower()
    assert "coaching" in prompt
    assert "courses" in prompt
    assert "mindset" in prompt
    assert "info-product" in prompt


def test_prompt_rejects_agencies_and_consultancies() -> None:
    prompt = build_prompt(
        company_name="Acme", description="d", cleaned_text="t"
    ).lower()
    assert "agency" in prompt
    assert "consultancy" in prompt


def test_prompt_rejects_long_established_and_lifestyle_businesses() -> None:
    prompt = build_prompt(
        company_name="Acme", description="d", cleaned_text="t"
    ).lower()
    assert "15+ years" in prompt or "decades" in prompt
    assert "lifestyle business" in prompt
    assert "local small business" in prompt


def test_prompt_guards_precision_when_unsure() -> None:
    """When genuinely unsure, the model must prefer null/true over false — a
    false exclusion hides a real startup, the failure mode that matters here."""
    prompt = build_prompt(
        company_name="Acme", description="d", cleaned_text="t"
    ).lower()
    assert "unsure" in prompt
    assert "null" in prompt
    # The cost framing: don't exclude a real startup.
    assert "hides" in prompt or "hide" in prompt


def test_prompt_keeps_us_vs_non_us_logic() -> None:
    """The country/HQ guidance must survive the eligibility tightening."""
    prompt = build_prompt(company_name="Acme", description="d", cleaned_text="t")
    assert "hq_country" in prompt
    assert "2-letter ISO code" in prompt


# ---------------------------------------------------------------------------
# EligibilityJudgment Pydantic round-trip
# ---------------------------------------------------------------------------


def test_judgment_all_fields_default_null() -> None:
    """Every field is optional/nullable — an empty object is the 'unsure' default
    and must NOT exclude anything."""
    obj = EligibilityJudgment.model_validate_json("{}")
    assert obj.is_startup is None
    assert obj.not_startup_reason is None
    assert obj.founded_year is None
    assert obj.hq_country is None


def test_judgment_parses_rejection() -> None:
    payload = {
        "is_startup": False,
        "not_startup_reason": "Online business directory, not a software product.",
        "founded_year": 2000,
        "hq_country": "US",
    }
    obj = EligibilityJudgment.model_validate_json(json.dumps(payload))
    assert obj.is_startup is False
    assert obj.not_startup_reason is not None
    assert obj.founded_year == 2000
    assert obj.hq_country == "US"
