"""Unit tests for source-verification pure logic (no DB, no LLM).

Covers the prompt schema's empty-not-fabricate discipline + the quote-grounding
guard (llm.prompts.source_verification) and the stage's pure helpers
(verify_sources: classify_source, the claim builders, _format_usd).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from nous.llm.prompts.source_verification import (
    SourceVerification,
    build_prompt,
    quote_is_grounded,
)
from nous.pipeline.verify_sources import (
    _format_usd,
    classify_source,
    funding_round_claim,
    status_claim,
    total_raised_claim,
)

# ── SourceVerification schema: quote discipline (empty-not-fabricate) ──────────


def test_supported_keeps_its_quote() -> None:
    v = SourceVerification(verdict="supported", supporting_quote="raised $12M")
    assert v.verdict == "supported"
    assert v.supporting_quote == "raised $12M"


def test_supported_without_quote_downgrades_to_uncertain() -> None:
    # A 'supported' with no quote can't be trusted — never a false ✓.
    v = SourceVerification(verdict="supported", supporting_quote=None)
    assert v.verdict == "uncertain"
    assert v.supporting_quote is None


def test_supported_with_blank_quote_downgrades() -> None:
    v = SourceVerification(verdict="supported", supporting_quote="   ")
    assert v.verdict == "uncertain"
    assert v.supporting_quote is None


def test_non_supported_verdicts_drop_any_quote() -> None:
    for verdict in ("unsupported", "uncertain"):
        v = SourceVerification(verdict=verdict, supporting_quote="stray quote")
        assert v.verdict == verdict
        assert v.supporting_quote is None


# ── quote_is_grounded: the anti-fabrication substring guard ────────────────────

_SOURCE = "TechCrunch reports that Acme has raised $12 million in a Series A round."


def test_grounded_exact_substring() -> None:
    assert quote_is_grounded("raised $12 million", _SOURCE) is True


def test_grounded_tolerates_whitespace_and_case() -> None:
    assert quote_is_grounded("RAISED   $12  MILLION", _SOURCE) is True


def test_not_grounded_when_absent() -> None:
    assert quote_is_grounded("raised $50 billion", _SOURCE) is False


def test_not_grounded_when_empty() -> None:
    assert quote_is_grounded(None, _SOURCE) is False
    assert quote_is_grounded("", _SOURCE) is False


# ── classify_source: verifiability buckets ─────────────────────────────────────


def test_classify_stored_wins_regardless_of_host() -> None:
    assert (
        classify_source("https://news.google.com/rss/x", has_stored_text=True)
        == "stored"
    )


def test_classify_google_news_without_text_is_unreachable() -> None:
    assert (
        classify_source("https://news.google.com/rss/articles/CBM", has_stored_text=False)
        == "unreachable"
    )


def test_classify_real_host_without_text_is_refetch() -> None:
    assert (
        classify_source("https://techcrunch.com/2026/acme", has_stored_text=False)
        == "refetch"
    )


def test_classify_hostless_url_is_unparseable() -> None:
    assert classify_source("not a url", has_stored_text=False) == "unparseable"
    assert classify_source(None, has_stored_text=False) == "unparseable"
    assert classify_source("", has_stored_text=False) == "unparseable"


# ── claim construction ─────────────────────────────────────────────────────────


def test_format_usd_scales() -> None:
    assert _format_usd(Decimal("12400000000")) == "$12.4B"
    assert _format_usd(Decimal("110000000")) == "$110M"
    assert _format_usd(Decimal("5500000")) == "$5.5M"
    assert _format_usd(Decimal("500000")) == "$500K"
    assert _format_usd(None) == "an undisclosed amount"
    # A negative (data-error) amount never fabricates a figure in the claim.
    assert _format_usd(Decimal("-100000")) == "an undisclosed amount"


def test_total_raised_claim_includes_amount_and_as_of() -> None:
    claim = total_raised_claim("Acme", Decimal("12000000"), date(2026, 1, 15))
    assert "Acme" in claim
    assert "$12.0M" in claim
    assert "2026-01-15" in claim


def test_status_claim_maps_lifecycle_phrases() -> None:
    assert status_claim("Acme", "acquired") == "Acme has been acquired."
    assert status_claim("Acme", "shut_down") == "Acme has shut down."
    assert status_claim("Acme", "ipo") == "Acme has gone public (IPO)."


def test_funding_round_claim_assembles_parts() -> None:
    claim = funding_round_claim(
        "Acme",
        Decimal("40000000"),
        "Series B",
        Decimal("400000000"),
        date(2026, 3, 1),
    )
    assert "Acme raised $40.0M" in claim
    assert "Series B round" in claim
    assert "$400M post-money valuation" in claim
    assert "announced 2026-03-01" in claim


# ── build_prompt: the claim + source land in the template ──────────────────────


def test_build_prompt_embeds_claim_and_source() -> None:
    prompt = build_prompt(claim="Acme raised $12M.", source_text="Some article body.")
    assert "Acme raised $12M." in prompt
    assert "Some article body." in prompt
    assert "verbatim" in prompt.lower()
