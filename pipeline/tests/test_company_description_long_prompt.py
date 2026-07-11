"""Tests for nous.llm.prompts.company_description_long (W-F)."""

from __future__ import annotations

import json
import re

from nous.llm.client import MAX_PROMPT_INPUT_CHARS
from nous.llm.prompts.company_description_long import (
    MAX_DESCRIPTION_INPUT_CHARS,
    PROMPT_VERSION,
    CompanyLongDescription,
    build_prompt,
)

# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_interpolates_name_and_text() -> None:
    prompt = build_prompt(company_name="Acme", cleaned_text="we build X")
    assert "Acme" in prompt
    assert "we build X" in prompt


def test_prompt_contract_dimensions_present() -> None:
    """The source-gated dimensions the W-F contract requires are all asked
    for explicitly."""
    prompt = build_prompt(company_name="Acme", cleaned_text="x").lower()
    for needle in (
        "the problem",
        "technical approach",
        "use cases",
        "business model",
        "market context",
        "distinctive",
        "traction",
    ):
        assert needle in prompt, f"contract dimension missing: {needle}"


def test_prompt_has_depth_floor_and_target() -> None:
    prompt = build_prompt(company_name="Acme", cleaned_text="x")
    assert "350-600 words" in prompt
    assert "4-7" in prompt
    assert "AT LEAST 4 substantial paragraphs" in prompt


def test_prompt_keeps_thin_site_honesty_front_and_center() -> None:
    """Never pad, never invent; null over filler — the guard that must
    survive every future edit of this prompt."""
    prompt = build_prompt(company_name="Acme", cleaned_text="x").lower()
    assert "never pad, never invent" in prompt
    assert "unknown stays unknown" in prompt
    assert '{"description_long": null}' in prompt
    assert "never estimate, extrapolate, or round numbers" in prompt


def test_prompt_forbids_marketing_fluff() -> None:
    prompt = build_prompt(company_name="Acme", cleaned_text="x").lower()
    assert "no marketing fluff" in prompt
    assert "empty adjectives" in prompt


def test_prompt_does_not_ask_for_classification() -> None:
    """The judge's duties must not creep back in."""
    prompt = build_prompt(company_name="Acme", cleaned_text="x")
    assert "do\nNOT classify" in prompt or "do NOT classify" in prompt
    for judge_field in ("is_startup", "website_state", "hq_city", "founded_year"):
        assert judge_field not in prompt


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_null_description_accepted() -> None:
    obj = CompanyLongDescription.model_validate_json(
        json.dumps({"description_long": None})
    )
    assert obj.description_long is None


def test_missing_description_defaults_to_null() -> None:
    obj = CompanyLongDescription.model_validate_json("{}")
    assert obj.description_long is None


def test_blank_description_normalizes_to_null() -> None:
    """Whitespace-only output is filler, not a profile."""
    obj = CompanyLongDescription.model_validate_json(
        json.dumps({"description_long": "   \n\n  "})
    )
    assert obj.description_long is None


def test_real_description_round_trips() -> None:
    text = "Para one.\n\nPara two."
    obj = CompanyLongDescription.model_validate_json(
        json.dumps({"description_long": text})
    )
    assert obj.description_long == text


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_version_scheme() -> None:
    assert re.match(r"^\d{4}-\d{2}-\d{2}\.\d+$", PROMPT_VERSION)


def test_describe_budget_exceeds_shared_ceiling_deliberately() -> None:
    """The describe call is the ONE documented exception above the shared
    input ceiling (see llm/client.py). If this ever flips, both comments
    are stale — fix them together."""
    assert MAX_DESCRIPTION_INPUT_CHARS > MAX_PROMPT_INPUT_CHARS
    assert MAX_DESCRIPTION_INPUT_CHARS == 48_000
