"""Tests for nous.llm.prompts.github_trending_company.

Pure prompt-construction + Pydantic round-trip tests; no DB required (the
``discover-github-trending`` stage integration lives in
``test_discover_github_trending.py``).
"""

from __future__ import annotations

import json
import re

from nous.llm.prompts.github_trending_company import (
    PROMPT_VERSION,
    TrendingCompanyJudgment,
    build_prompt,
    format_repos_block,
)

# ---------------------------------------------------------------------------
# PROMPT_VERSION — shared scheme
# ---------------------------------------------------------------------------


def test_prompt_version_uses_shared_scheme() -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", PROMPT_VERSION)


# ---------------------------------------------------------------------------
# build_prompt — interpolation
# ---------------------------------------------------------------------------


def _prompt(**overrides: object) -> str:
    kwargs: dict = {
        "owner_login": "acme",
        "account_type": "Organization",
        "profile_name": "Acme Inc",
        "profile_website": "https://acme.dev",
        "profile_bio": "Widgets as a service",
        "repos_block": "- widget: A widget engine [language: Rust, stars: 1200]",
    }
    kwargs.update(overrides)
    return build_prompt(**kwargs)


def test_build_prompt_contains_all_inputs() -> None:
    prompt = _prompt()
    assert "acme" in prompt
    assert "Organization" in prompt
    assert "Acme Inc" in prompt
    assert "https://acme.dev" in prompt
    assert "Widgets as a service" in prompt
    assert "- widget: A widget engine [language: Rust, stars: 1200]" in prompt


def test_build_prompt_renders_unknown_for_missing_profile() -> None:
    """A failed profile fetch must read as an explicit unknown, not as an
    empty field the model might over-interpret."""
    prompt = _prompt(
        account_type=None, profile_name=None, profile_website=None, profile_bio=None
    )
    assert "Account type: (unknown)" in prompt
    assert "Profile name: (unknown)" in prompt
    assert "Profile website: (unknown)" in prompt


# ---------------------------------------------------------------------------
# Rejection categories + the no-fabrication guard, pinned so a future prompt
# loosening trips a test rather than silently regressing (same pattern as
# test_company_eligibility_prompt.py).
# ---------------------------------------------------------------------------


def test_prompt_rejects_the_exclusion_classes() -> None:
    prompt = _prompt().lower()
    assert "personal" in prompt
    assert "foundation" in prompt
    assert "university" in prompt
    assert "research lab" in prompt
    assert "big-tech" in prompt
    assert "non-us" in prompt


def test_prompt_requires_null_on_uncertainty() -> None:
    prompt = _prompt().lower()
    assert "null" in prompt
    assert "never" in prompt and "guess" in prompt


def test_prompt_forbids_inventing_names() -> None:
    prompt = _prompt()
    assert "NEVER" in prompt
    assert "not derived from" in prompt


# ---------------------------------------------------------------------------
# format_repos_block
# ---------------------------------------------------------------------------


def test_format_repos_block_renders_details_and_defaults() -> None:
    block = format_repos_block(
        [
            ("widget", "A widget engine", "Rust", 1200),
            ("nodesc", None, None, None),
            ("langonly", "Tool", "Go", None),
        ]
    )
    lines = block.splitlines()
    assert lines[0] == "- widget: A widget engine [language: Rust, stars: 1200]"
    assert lines[1] == "- nodesc: (no description)"
    assert lines[2] == "- langonly: Tool [language: Go]"


# ---------------------------------------------------------------------------
# Schema round-trip — what the LLM returns must validate
# ---------------------------------------------------------------------------


def test_judgment_roundtrip_accept() -> None:
    judgment = TrendingCompanyJudgment.model_validate_json(
        json.dumps(
            {
                "is_company": True,
                "company_name": "Acme",
                "reason": "Open-core devtool with a hosted cloud product.",
            }
        )
    )
    assert judgment.is_company is True
    assert judgment.company_name == "Acme"


def test_judgment_roundtrip_all_null_is_valid() -> None:
    """Uncertainty (all-null) must validate — null is the safe default."""
    judgment = TrendingCompanyJudgment.model_validate_json("{}")
    assert judgment.is_company is None
    assert judgment.company_name is None
    assert judgment.reason is None
