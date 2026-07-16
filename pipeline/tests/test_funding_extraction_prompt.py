"""Tests for nous.llm.prompts.funding_extraction.

Pure prompt-building + Pydantic schema validation. No LLM call.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from nous.llm.prompts.funding_extraction import (
    MAX_ARTICLE_CHARS,
    FundingExtraction,
    build_prompt,
    build_website_prompt,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ARTICLE_FIXTURE = FIXTURES_DIR / "article_funding_announcement.txt"


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_contains_company_name_and_article_text() -> None:
    prompt = build_prompt(company_name="X", article_text="Y")
    assert "X" in prompt
    assert "Y" in prompt


def test_build_prompt_truncates_long_article_text() -> None:
    """article_text longer than MAX_ARTICLE_CHARS is truncated to that cap."""
    long_text = "a" * 50_000
    prompt = build_prompt(company_name="TestCo", article_text=long_text)
    # The article body appears once inside the template; everything beyond
    # MAX_ARTICLE_CHARS of 'a's must be gone.
    assert "a" * MAX_ARTICLE_CHARS in prompt
    assert "a" * (MAX_ARTICLE_CHARS + 1) not in prompt
    # Sanity: the rest of the template is a small fixed overhead, so the
    # rendered prompt length is the truncated body plus the template scaffolding.
    # (Ceiling bumped 2,000 → 2,500 when the total_raised_usd rule landed,
    # → 3,000 for the Task A4 always-capture-valuation rule, → 3,500 for the
    # 2026-07-16 in-talks rumor guard; it still guards runaway template bloat.)
    template_overhead = len(prompt) - MAX_ARTICLE_CHARS
    assert 0 < template_overhead < 3_500


def test_build_prompt_short_article_is_unchanged() -> None:
    """Articles under the cap are passed through verbatim."""
    body = "Short article body about a Series A round."
    prompt = build_prompt(company_name="Acme", article_text=body)
    assert body in prompt


# ---------------------------------------------------------------------------
# build_website_prompt (fallback source)
# ---------------------------------------------------------------------------


def test_build_website_prompt_contains_company_and_text() -> None:
    prompt = build_website_prompt(company_name="Acme", page_text="we raised $20M")
    assert "Acme" in prompt
    assert "we raised $20M" in prompt


def test_build_website_prompt_instructs_latest_date_and_own_site() -> None:
    """The website variant flags lower authority + prefer the most recent date."""
    prompt = build_website_prompt(company_name="Acme", page_text="x")
    assert "OWN public website" in prompt
    assert "MOST" in prompt and "RECENT" in prompt
    assert "Company website" in prompt


def test_build_website_prompt_truncates() -> None:
    long_text = "b" * 50_000
    prompt = build_website_prompt(company_name="Acme", page_text=long_text)
    assert "b" * MAX_ARTICLE_CHARS in prompt
    assert "b" * (MAX_ARTICLE_CHARS + 1) not in prompt


# ---------------------------------------------------------------------------
# FundingExtraction Pydantic round-trip — positive case
# ---------------------------------------------------------------------------


VALID_FUNDING_PAYLOAD = {
    "is_funding_announcement": True,
    "round_type": "Series A",
    "amount_raised_usd": 50000000,
    "valuation_post_money_usd": 300000000,
    "announced_date": "2026-01-15",
    "lead_investors": ["Lightspeed"],
    "other_investors": ["Founders Fund", "a16z"],
    "confidence": "high",
}


def test_funding_extraction_accepts_valid_payload() -> None:
    obj = FundingExtraction.model_validate_json(json.dumps(VALID_FUNDING_PAYLOAD))
    assert obj.is_funding_announcement is True
    assert obj.round_type == "Series A"
    assert obj.amount_raised_usd == Decimal("50000000")
    assert obj.valuation_post_money_usd == Decimal("300000000")
    assert obj.announced_date == date(2026, 1, 15)
    assert obj.lead_investors == ["Lightspeed"]
    assert obj.other_investors == ["Founders Fund", "a16z"]
    assert obj.confidence == "high"


# ---------------------------------------------------------------------------
# FundingExtraction Pydantic round-trip — negative-classification case
# ---------------------------------------------------------------------------


NOT_FUNDING_PAYLOAD = {
    "is_funding_announcement": False,
    "round_type": None,
    "amount_raised_usd": None,
    "valuation_post_money_usd": None,
    "announced_date": None,
    "lead_investors": [],
    "other_investors": [],
    "confidence": "low",
}


def test_funding_extraction_accepts_not_funding_announcement() -> None:
    obj = FundingExtraction.model_validate_json(json.dumps(NOT_FUNDING_PAYLOAD))
    assert obj.is_funding_announcement is False
    assert obj.round_type is None
    assert obj.amount_raised_usd is None
    assert obj.valuation_post_money_usd is None
    assert obj.announced_date is None
    assert obj.lead_investors == []
    assert obj.other_investors == []
    assert obj.confidence == "low"


# ---------------------------------------------------------------------------
# FundingExtraction — rejects invalid confidence
# ---------------------------------------------------------------------------


def test_funding_extraction_rejects_invalid_confidence() -> None:
    """confidence is a Literal['low', 'medium', 'high']; other values are rejected."""
    payload = {**VALID_FUNDING_PAYLOAD, "confidence": "very high"}
    with pytest.raises(ValidationError):
        FundingExtraction.model_validate_json(json.dumps(payload))


# ---------------------------------------------------------------------------
# Status-event fields (acquired / shut_down / ipo)
# ---------------------------------------------------------------------------


def test_status_fields_default_to_none_on_legacy_payloads() -> None:
    """Payloads predating status_event/status_confidence still validate, and
    both fields default to None — old fixtures and cached LLM payloads must
    keep parsing unchanged."""
    obj = FundingExtraction.model_validate_json(json.dumps(VALID_FUNDING_PAYLOAD))
    assert obj.status_event is None
    assert obj.status_confidence is None

    neg = FundingExtraction.model_validate_json(json.dumps(NOT_FUNDING_PAYLOAD))
    assert neg.status_event is None
    assert neg.status_confidence is None


def test_status_event_round_trips() -> None:
    """An acquisition signal on a NON-funding article (the common case) parses
    and survives a dump/validate round-trip."""
    payload = {
        **NOT_FUNDING_PAYLOAD,
        "status_event": "acquired",
        "status_confidence": "high",
    }
    obj = FundingExtraction.model_validate_json(json.dumps(payload))
    assert obj.is_funding_announcement is False
    assert obj.status_event == "acquired"
    assert obj.status_confidence == "high"

    again = FundingExtraction.model_validate(obj.model_dump())
    assert again.status_event == "acquired"
    assert again.status_confidence == "high"


def test_status_event_rejects_unknown_value() -> None:
    """status_event is a Literal['acquired','shut_down','ipo'] | None."""
    payload = {**NOT_FUNDING_PAYLOAD, "status_event": "merged"}
    with pytest.raises(ValidationError):
        FundingExtraction.model_validate_json(json.dumps(payload))


def test_status_confidence_rejects_unknown_value() -> None:
    payload = {
        **NOT_FUNDING_PAYLOAD,
        "status_event": "ipo",
        "status_confidence": "certain",
    }
    with pytest.raises(ValidationError):
        FundingExtraction.model_validate_json(json.dumps(payload))


def test_news_prompt_contains_status_rules() -> None:
    """The news template must ask for status events even on non-funding
    articles, and must forbid guessing (rumors / other companies' exits)."""
    prompt = build_prompt(company_name="Acme", article_text="x")
    assert "status_event" in prompt
    assert "even when is_funding_announcement is false" in prompt
    assert "Never guess" in prompt


def test_website_prompt_contains_status_rule_with_medium_cap() -> None:
    """The website template allows status events only for explicit own-site
    notices and caps status_confidence at 'medium'."""
    prompt = build_website_prompt(company_name="Acme", page_text="x")
    assert "status_event" in prompt
    assert "cap status_confidence at 'medium'" in prompt
    assert "Never guess" in prompt


def test_news_prompt_contains_acquirer_direction_rule() -> None:
    """"Acme acquires SmallCo" must never brand Acme as 'acquired' — and
    never-downgrade makes a wrong 'acquired' sticky. The news template must
    state that the named company being the BUYER is not a status event."""
    prompt = build_prompt(company_name="Acme", article_text="x")
    assert "If Acme is the acquirer (it bought another company)" in prompt
    assert "Acme itself is the company being bought" in prompt


def test_website_prompt_contains_acquirer_direction_rule() -> None:
    """Same direction risk on own-site "we acquired X" posts: the website
    template must rule out buyer-side acquisitions too."""
    prompt = build_website_prompt(company_name="Acme", page_text="x")
    assert 'Acme acquired ANOTHER company ("we acquired X")' in prompt
    assert "Acme itself is the company being bought" in prompt


def test_status_event_schema_description_carries_direction_clause() -> None:
    """Field descriptions ship to the model inside the JSON schema
    (complete_json embeds model_json_schema() in the system prompt), so the
    buyer-direction rule must live there as well as in the templates."""
    schema = FundingExtraction.model_json_schema()
    desc = schema["properties"]["status_event"]["description"]
    assert "If the named company is the acquirer" in desc
    assert "named company itself is the company being bought" in desc


# ---------------------------------------------------------------------------
# Stated cumulative total (total_raised_usd)
# ---------------------------------------------------------------------------


def test_total_raised_defaults_to_none_on_legacy_payloads() -> None:
    """Payloads predating total_raised_usd still validate, defaulting to None —
    old fixtures and cached LLM payloads must keep parsing unchanged (same
    compat contract as the status_event fields)."""
    obj = FundingExtraction.model_validate_json(json.dumps(VALID_FUNDING_PAYLOAD))
    assert obj.total_raised_usd is None

    neg = FundingExtraction.model_validate_json(json.dumps(NOT_FUNDING_PAYLOAD))
    assert neg.total_raised_usd is None


def test_total_raised_round_trips() -> None:
    """A stated cumulative total parses and survives a dump/validate round-trip
    — including on a NON-funding payload (totals appear in acquisition
    coverage too)."""
    payload = {**NOT_FUNDING_PAYLOAD, "total_raised_usd": 285000000}
    obj = FundingExtraction.model_validate_json(json.dumps(payload))
    assert obj.is_funding_announcement is False
    assert obj.total_raised_usd == Decimal("285000000")

    again = FundingExtraction.model_validate(obj.model_dump())
    assert again.total_raised_usd == Decimal("285000000")


def test_news_prompt_contains_total_raised_rule() -> None:
    """The news template must ask for an article-STATED cumulative total —
    even on non-funding articles — and forbid the model computing one."""
    prompt = build_prompt(company_name="Acme", article_text="x")
    assert "total_raised_usd" in prompt
    assert "to date" in prompt
    assert "never sum or infer one yourself" in prompt
    assert "amount_raised_usd (the round being announced)" in prompt
    # Totals appear in non-funding coverage (e.g. acquisition articles), so the
    # rule must carve out the "leave other fields null" instruction.
    assert prompt.count("even when is_funding_announcement is false") >= 2


def test_website_prompt_contains_total_raised_rule() -> None:
    """The website template carries the equivalent rule — sites often state
    "we've raised $X" without it being a round announcement."""
    prompt = build_website_prompt(company_name="Acme", page_text="x")
    assert "total_raised_usd" in prompt
    assert "never sum" in prompt


# ---------------------------------------------------------------------------
# Task A4 — post-money valuation + stated total emphasis
# ---------------------------------------------------------------------------


def test_news_prompt_emphasizes_post_money_valuation() -> None:
    """The news template must tell the model to ALWAYS capture the post-money
    valuation and its source when the article states one (Task A4) — these are
    captured only opportunistically today. Still "null over guess"."""
    prompt = build_prompt(company_name="Acme", article_text="x")
    assert "Always capture" in prompt
    assert "post-money valuation" in prompt
    assert "valuation_post_money_usd" in prompt
    # The null-over-guess guard must survive the stronger phrasing.
    assert "do not invent" in prompt.lower()


def test_website_prompt_emphasizes_post_money_valuation() -> None:
    """The website template carries the same always-capture-valuation rule."""
    prompt = build_website_prompt(company_name="Acme", page_text="x")
    assert "Always capture" in prompt
    assert "post-money valuation" in prompt
    assert "valuation_post_money_usd" in prompt


def test_valuation_field_description_carries_post_money_emphasis() -> None:
    """The field description (embedded in the JSON schema sent to the model)
    must also instruct always-capture, not just the prompt template."""
    schema = FundingExtraction.model_json_schema()
    desc = schema["properties"]["valuation_post_money_usd"]["description"]
    assert "post-money valuation" in desc.lower()
    assert "Always" in desc


# A representative LLM payload for the A4 acceptance scenario:
# "Series B of $40M at a $400M post-money valuation, bringing total raised to
# $58M". Asserts the FundingExtraction schema parses exactly what a compliant
# model returns for that sentence — no live DeepSeek call.
A4_VALUATION_TOTAL_PAYLOAD = {
    "is_funding_announcement": True,
    "round_type": "Series B",
    "amount_raised_usd": 40000000,
    "valuation_post_money_usd": 400000000,
    "valuation_source": "TechCrunch, June 2026",
    "announced_date": None,
    "lead_investors": ["Acme Growth"],
    "other_investors": [],
    "confidence": "high",
    "total_raised_usd": 58000000,
}


def test_funding_extraction_parses_valuation_and_total_fixture() -> None:
    """The representative A4 payload (round + post-money valuation + source +
    stated cumulative total) round-trips through the Pydantic model."""
    obj = FundingExtraction.model_validate_json(json.dumps(A4_VALUATION_TOTAL_PAYLOAD))
    assert obj.round_type == "Series B"
    assert obj.amount_raised_usd == Decimal("40000000")
    assert obj.valuation_post_money_usd == Decimal("400000000")
    assert obj.valuation_source == "TechCrunch, June 2026"
    assert obj.total_raised_usd == Decimal("58000000")

    again = FundingExtraction.model_validate(obj.model_dump())
    assert again.valuation_post_money_usd == Decimal("400000000")
    assert again.total_raised_usd == Decimal("58000000")


# ---------------------------------------------------------------------------
# Task 2.6 — announced_date: no fabricated Jan-1 placeholder
# ---------------------------------------------------------------------------


def test_news_prompt_instructs_specific_day_only_for_announced_date() -> None:
    """The news template must tell the model to return null when only a month
    or year is known — not fabricate the 1st as a placeholder."""
    prompt = build_prompt(company_name="Acme", article_text="x")
    assert "SPECIFIC day" in prompt
    # The template wraps at column width; check the key instruction words are
    # all present regardless of line breaks.
    assert "do NOT" in prompt
    assert "default to the 1st" in prompt
    assert "month or year is known" in prompt


def test_website_prompt_instructs_specific_day_only_for_announced_date() -> None:
    """The website template carries the same no-fabricated-date rule."""
    prompt = build_website_prompt(company_name="Acme", page_text="x")
    assert "SPECIFIC day" in prompt
    assert "do NOT" in prompt
    assert "default to the 1st" in prompt
    assert "month or year is known" in prompt


def test_announced_date_field_description_instructs_specific_day() -> None:
    """The field description (embedded in JSON schema for the model) must also
    carry the no-fabrication instruction — not just the prompt template."""
    schema = FundingExtraction.model_json_schema()
    desc = schema["properties"]["announced_date"]["description"]
    assert "SPECIFIC day" in desc
    assert "do NOT guess or default to the 1st" in desc


def test_year_only_article_yields_null_announced_date_via_schema() -> None:
    """Simulates the LLM returning null for announced_date when an article says
    only 'in 2024'.  The model should return null; this asserts the Pydantic
    schema accepts null (i.e. won't reject a compliant LLM response)."""
    payload = {
        "is_funding_announcement": True,
        "round_type": "Seed",
        "amount_raised_usd": 5000000,
        "valuation_post_money_usd": None,
        "announced_date": None,  # only "in 2024" stated — model returns null
        "lead_investors": [],
        "other_investors": [],
        "confidence": "medium",
    }
    obj = FundingExtraction.model_validate_json(json.dumps(payload))
    assert obj.announced_date is None, (
        "When an article says only 'in 2024', the model should return null "
        "for announced_date, not a fabricated Jan-1 placeholder."
    )


# ---------------------------------------------------------------------------
# Snapshot test against the real article fixture
# ---------------------------------------------------------------------------


def test_build_prompt_snapshot_against_fixture() -> None:
    """Render the prompt against the committed article fixture.

    Asserts:
    - rendered length sits in a sane range,
    - both interpolation anchors are present,
    - the rules block survived the format() call.
    """
    article_text = ARTICLE_FIXTURE.read_text(encoding="utf-8")
    # Sanity-check the fixture itself so a test failure points at the right file.
    assert 500 <= len(article_text) <= MAX_ARTICLE_CHARS, (
        f"Fixture is outside the expected size range: {len(article_text)} chars"
    )

    prompt = build_prompt(company_name="Test Co", article_text=article_text)

    assert 500 <= len(prompt) <= 35_000
    assert "Company name being asked about: Test Co" in prompt
    assert "Article body:" in prompt
    # A distinctive substring from the article body — confirms the body
    # actually made it into the rendered prompt.
    assert "Recursive AI" in prompt
    # A line from the rules block — guards against accidental template edits
    # that drop the "do not invent" instruction.
    assert "Do not invent numbers." in prompt
