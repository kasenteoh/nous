"""Tests for the theme_naming prompt (compute-themes' one LLM call)."""

from __future__ import annotations

import re

from nous.llm.prompts.theme_naming import (
    MAX_MEMBERS,
    PROMPT_VERSION,
    ThemeNaming,
    build_prompt,
    format_members_block,
)


def test_prompt_version_scheme() -> None:
    # "<date>.<same-day-counter>" — the 0031 stamp convention.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", PROMPT_VERSION)


def test_schema_accepts_full_naming() -> None:
    naming = ThemeNaming.model_validate(
        {"name": "AI code review", "description": "Tools that review PRs."}
    )
    assert naming.name == "AI code review"
    assert naming.description == "Tools that review PRs."


def test_schema_defaults_to_null_over_fabricate() -> None:
    # An empty object (or explicit nulls) is the incoherent-cluster answer;
    # the stage drops the cluster rather than inventing a theme.
    assert ThemeNaming.model_validate({}).name is None
    naming = ThemeNaming.model_validate({"name": None, "description": None})
    assert naming.name is None
    assert naming.description is None


def test_members_block_renders_name_and_description() -> None:
    block = format_members_block(
        [("Acme", "Builds widgets."), ("Beta", None)]
    )
    assert "- Acme: Builds widgets." in block
    # Missing description renders an explicit marker, never an empty string.
    assert "- Beta: (no description)" in block


def test_members_block_caps_members_and_notes_remainder() -> None:
    members = [(f"Co {i}", "Does things.") for i in range(MAX_MEMBERS + 5)]
    block = format_members_block(members)
    assert block.count("\n") == MAX_MEMBERS  # MAX_MEMBERS lines + the note
    assert "and 5 more similar companies" in block


def test_members_block_truncates_long_descriptions() -> None:
    block = format_members_block([("Acme", "x" * 500)])
    line = block.splitlines()[0]
    assert len(line) < 250
    assert line.endswith("…")


def test_build_prompt_grounds_in_industry_and_members() -> None:
    prompt = build_prompt(
        industry_group="DevTools",
        members_block="- Acme: Builds widgets.",
    )
    assert '"DevTools"' in prompt
    assert "- Acme: Builds widgets." in prompt
    # The null-over-fabricate instruction is load-bearing — pin it.
    assert "return null for BOTH fields" in prompt
