"""Unit tests for nous.util.title_subject — the dominant-subject and
description-mismatch heuristics that back the wrong-website fix.

Two production incidents drive these:
- Kalshi (a prediction market) rendered FrenFlow's description because the
  resolver landed on FrenFlow's site, which merely LISTS Kalshi as a venue.
- AgentMail rendered a "Series V" description.

The cardinal rule: correctly-matched companies (subject == company) must NEVER be
flagged, while these list-among-others / wrong-leading-brand cases MUST be.
"""

from __future__ import annotations

import pytest

from nous.util.title_subject import (
    description_opening_subject,
    description_subject_mismatches,
    name_is_dominant_subject,
    names_refer_to_same,
)

# ---------------------------------------------------------------------------
# name_is_dominant_subject — ACCEPT legitimate single-subject titles/h1s
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "company"),
    [
        # Bare brand heading.
        ("Kalshi", "Kalshi"),
        ("Kalshi Inc", "Kalshi"),
        # Brand + tagline across every common separator.
        ("Kalshi — Trade on the outcome of events", "Kalshi"),
        ("Acme | Pricing", "Acme"),
        ("Acme · Developer Tools", "Acme"),
        ("Acme :: Home", "Acme"),
        ("Acme - The platform for teams", "Acme"),
        ("Acme • Product", "Acme"),
        ("Acme › Docs", "Acme"),
        ("Acme / Blog", "Acme"),
        # Leading boilerplate before the brand.
        ("Welcome to Acme", "Acme"),
        ("Home | Acme", "Acme"),
        ("Official site of Acme Robotics", "Acme Robotics"),
        # Corporate suffix mismatch between title and stored name.
        ("Acme, Inc.", "Acme"),
        ("Acme — home", "Acme Inc"),
        # Comparison page is still about the subject (it leads the list).
        ("Acme vs Bar", "Acme"),
        # Descriptive sentence-style h1 that opens with the brand.
        ("Acme helps engineering teams ship faster", "Acme"),
        ("Kalshi is the first regulated prediction market in the US", "Kalshi"),
        # Multi-token brand as the leading segment.
        ("Lightning AI — The all-in-one platform", "Lightning AI"),
        # Brand contains a dot the slug form drops.
        ("Predict.fun | Prediction markets", "Predict.fun"),
    ],
)
def test_dominant_subject_accepts_legit(text: str, company: str) -> None:
    assert name_is_dominant_subject(text, company) is True


# ---------------------------------------------------------------------------
# name_is_dominant_subject — REJECT wrong-leading-brand / list-among-others
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "company"),
    [
        # The Kalshi/FrenFlow incident: leading brand is a different company.
        ("FrenFlow — Multi-Venue Prediction Market Platform", "Kalshi"),
        # The AgentMail/Series V incident.
        ("Series V — Capital for technical founders", "AgentMail"),
        ("Series V", "AgentMail"),
        # Company appears, but only as a non-first item in a brand list.
        (
            "Copy-trade across Polymarket, Kalshi, Predict.fun and Hyperliquid",
            "Kalshi",
        ),
        ("Polymarket, Kalshi & Predict.fun", "Kalshi"),
        ("Stripe, Adyen and Braintree compared", "Adyen"),
        # Plain different brand, no separator.
        ("FrenFlow", "Kalshi"),
        ("Notion", "Coda"),
    ],
)
def test_dominant_subject_rejects_wrong_or_listed(text: str, company: str) -> None:
    assert name_is_dominant_subject(text, company) is False


def test_dominant_subject_first_in_list_accepted() -> None:
    """When the company IS the first listed brand, it is the dominant subject."""
    assert (
        name_is_dominant_subject("Kalshi, Polymarket and Predict.fun", "Kalshi")
        is True
    )


def test_dominant_subject_empty_inputs() -> None:
    assert name_is_dominant_subject("", "Kalshi") is False
    assert name_is_dominant_subject("Kalshi", "") is False
    assert name_is_dominant_subject("   ", "Kalshi") is False


# ---------------------------------------------------------------------------
# description_opening_subject — extract the named subject of a blurb
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            "FrenFlow is a multi-venue prediction-market platform that lets you "
            "copy-trade across Polymarket, Kalshi, Predict.fun, Hyperliquid.",
            "FrenFlow",
        ),
        ("Series V provides capital to technical founders.", "Series V"),
        ("Ramp is an all-in-one spend management platform.", "Ramp"),
        ("Lightning AI offers an end-to-end ML platform.", "Lightning AI"),
        ("Predict.fun builds on-chain prediction markets.", "Predict.fun"),
        ("AgentMail provides an email API for AI agents.", "AgentMail"),
    ],
)
def test_description_opening_subject_extracts(
    description: str, expected: str
) -> None:
    assert description_opening_subject(description) == expected


@pytest.mark.parametrize(
    "description",
    [
        "An AI platform for engineering teams.",
        "We help engineering teams ship faster.",
        "A serverless inference platform for generative AI.",
        "Founded in 2021, headquartered in San Francisco.",
        "",
        "   ",
    ],
)
def test_description_opening_subject_unrecognized_returns_none(
    description: str,
) -> None:
    assert description_opening_subject(description) is None


# ---------------------------------------------------------------------------
# description_subject_mismatches — the conservative repair signal
# ---------------------------------------------------------------------------


def test_mismatch_flags_frenflow_for_kalshi() -> None:
    """The exact production bug: Kalshi's stored description is about FrenFlow."""
    desc = (
        "FrenFlow is a multi-venue prediction-market platform that lets you "
        "copy-trade the sharpest traders across Polymarket, Kalshi, Predict.fun, "
        "and Hyperliquid from one dashboard."
    )
    assert description_subject_mismatches(desc, "Kalshi") is True


def test_mismatch_flags_series_v_for_agentmail() -> None:
    desc = "Series V provides early-stage capital to technical founders."
    assert description_subject_mismatches(desc, "AgentMail") is True


@pytest.mark.parametrize(
    ("description", "company"),
    [
        # Correctly-matched companies — subject IS the company. NEVER flag.
        ("Ramp is an all-in-one spend management platform.", "Ramp"),
        (
            "Kalshi is the first regulated prediction market in the US.",
            "Kalshi",
        ),
        ("Lightning AI offers an end-to-end ML platform.", "Lightning AI"),
        # Stored name carries a suffix the description omits (and vice-versa).
        ("Ramp is a spend platform.", "Ramp Financial"),
        ("Ramp Financial is a spend platform.", "Ramp"),
        ("Kalshi Inc operates a prediction market.", "Kalshi"),
        # Stylization / punctuation differences.
        ("Predict.fun builds on-chain prediction markets.", "Predict.fun"),
        ("PredictFun builds on-chain prediction markets.", "Predict.fun"),
        # Unrecognized opener → no extractable subject → no mismatch asserted.
        ("An AI platform for engineering teams.", "Acme"),
        ("We build developer tools.", "Acme"),
        ("A spend management platform.", "Ramp"),
    ],
)
def test_mismatch_never_flags_correct_or_unknown(
    description: str, company: str
) -> None:
    assert description_subject_mismatches(description, company) is False


# ---------------------------------------------------------------------------
# names_refer_to_same
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("Kalshi", "Kalshi", True),
        ("Kalshi", "Kalshi Inc", True),
        ("Ramp", "Ramp Financial", True),
        ("Predict.fun", "PredictFun", True),
        ("Acme, Inc.", "Acme", True),
        ("Kalshi", "FrenFlow", False),
        ("AgentMail", "Series V", False),
        # Short tokens must not spuriously contain.
        ("AI", "Lightning AI", False),
        ("X", "Xometry", False),
        ("", "Kalshi", False),
    ],
)
def test_names_refer_to_same(a: str, b: str, expected: bool) -> None:
    assert names_refer_to_same(a, b) is expected
