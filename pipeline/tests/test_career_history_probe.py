"""Unit tests for the career-history-probe pure regex helpers (no DB required).

These pin the Tier-1 signal / Tier-2 named-employer / bio-marker semantics that
the SQL aggregate path relies on via the SAME module constants. DB-gated tests
live in tests/test_career_history_probe_db.py.
"""

from __future__ import annotations

import pytest

from nous.pipeline.career_history_probe import (
    capture_prior_employers,
    has_bio_section,
    has_career_signal,
    has_named_prior,
)

# --- Tier 1 — broad career-history signal -----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Previously led developer productivity at a large company.",
        "she was previously a partner",
        "Formerly a staff engineer on build systems.",
        "ex-Google engineer who built things",
        "an ex-Stripe operator",
        "prior to founding the company",
        "before joining, he ran ops",
        "worked at three startups",
        "spent 8 years in infra",
        "12 years at a bank",
        "veteran of the payments world",
        "an early engineer at a unicorn",
        "co-founded two companies",
    ],
)
def test_tier1_detects_career_prose(text: str) -> None:
    assert has_career_signal(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "We build great developer tools.",
        "Our platform runs at scale.",
        "A complex-systems research lab.",  # 'complex-' must NOT trip the ex- rule
        "Reflexology and wellness services.",
    ],
)
def test_tier1_ignores_non_career_prose(text: str) -> None:
    assert has_career_signal(text) is False


# --- Tier 2 — NAMED prior employer capture ----------------------------------


def test_tier2_captures_from_ex_prefix() -> None:
    assert capture_prior_employers("ex-Stripe") == ["Stripe"]


def test_tier2_captures_from_previously_at() -> None:
    assert capture_prior_employers("previously at Google") == ["Google"]


def test_tier2_captures_sentence_start_cue() -> None:
    # Case-insensitive cue matching still registers a capitalized "Previously".
    assert capture_prior_employers("Previously at Google, she led growth.") == [
        "Google"
    ]


def test_tier2_captures_multiword_name() -> None:
    assert capture_prior_employers("previously at Goldman Sachs") == ["Goldman Sachs"]


@pytest.mark.parametrize(
    "text",
    [
        "previously at a large fintech",  # described, not named → no capital
        "formerly a staff engineer on build systems",
        "built the platform at scale",  # 'at scale' is noise, no named org
        "operated at scale for years",
        "we serve customers at enterprise scale",
    ],
)
def test_tier2_does_not_capture_unnamed_or_noise(text: str) -> None:
    assert capture_prior_employers(text) == []
    assert has_named_prior(text) is False


def test_tier2_dedupes_case_insensitively_and_preserves_order() -> None:
    text = "ex-Stripe. Later, previously at Stripe again, then ex-Google."
    assert capture_prior_employers(text) == ["Stripe", "Google"]


def test_has_named_prior_matches_capture() -> None:
    assert has_named_prior("ex-Stripe founder") is True
    assert has_named_prior("a purely descriptive bio") is False


# --- Bio-presence marker ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Maya Okafor — Co-founder & CEO",
        "Jane Doe, CTO",
        "our COO joined last year",
        "Chief Technology Officer",
        "Meet the team",
        "Our leadership",
        "cofounder and head of product",
    ],
)
def test_bio_marker_detects_leadership_sections(text: str) -> None:
    assert has_bio_section(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "We cooperate with partners across the ecosystem.",  # 'coo' in cooperate
        "A ceothermal energy startup.",  # 'ceo' mid-word must not trip
        "Fast, reliable infrastructure.",
    ],
)
def test_bio_marker_ignores_incidental_substrings(text: str) -> None:
    assert has_bio_section(text) is False
