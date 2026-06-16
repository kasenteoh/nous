"""Unit tests for the focused HQ-country prompt (no DB, no network)."""

from __future__ import annotations

from nous.llm.prompts.hq_country import HqCountryJudgment, build_prompt


def test_judgment_defaults_to_unknown() -> None:
    j = HqCountryJudgment()
    assert j.hq_country is None
    assert j.evidence_quote is None


def test_judgment_parses_country_and_quote() -> None:
    j = HqCountryJudgment.model_validate(
        {"hq_country": "DK", "evidence_quote": "Fullview ApS, Copenhagen"}
    )
    assert j.hq_country == "DK"
    assert j.evidence_quote == "Fullview ApS, Copenhagen"


def test_prompt_includes_company_name_and_customer_guard() -> None:
    prompt = build_prompt(
        company_name="Acme",
        description="Does things.",
        cleaned_text="Acme GmbH, Berlin.",
    )
    assert "Acme" in prompt
    assert "Acme GmbH, Berlin." in prompt
    # The dominant false-positive guard MUST be present in the instructions.
    assert "customers" in prompt.lower()
    assert "ignore" in prompt.lower()
