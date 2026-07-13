"""Unit tests for nous.util.us_state.canonical_us_state (pure, no DB).

Pins the canonical form (2-letter UPPERCASE USPS code) and the contract that
non-US / unrecognized values return None so callers leave them untouched.
"""

from __future__ import annotations

import pytest

from nous.util.us_state import (
    US_STATE_CODE_TO_NAME,
    US_STATE_CODES,
    US_STATE_NAME_TO_CODE,
    canonical_us_state,
)


def test_map_covers_50_states_plus_dc() -> None:
    assert len(US_STATE_CODE_TO_NAME) == 51  # 50 states + DC
    assert "DC" in US_STATE_CODE_TO_NAME
    assert US_STATE_CODE_TO_NAME["DC"] == "District of Columbia"
    # Codes are the frozenset of keys.
    assert frozenset(US_STATE_CODE_TO_NAME) == US_STATE_CODES
    # Every code round-trips through the reverse name map.
    for code, name in US_STATE_CODE_TO_NAME.items():
        assert US_STATE_NAME_TO_CODE[name.lower()] == code


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("California", "CA"),
        ("california", "CA"),
        ("CALIFORNIA", "CA"),
        ("  California  ", "CA"),
        ("New York", "NY"),
        ("new york", "NY"),
        ("North Dakota", "ND"),
    ],
)
def test_full_name_maps_to_code(raw: str, expected: str) -> None:
    assert canonical_us_state(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CA", "CA"),
        ("ca", "CA"),
        ("Ca", "CA"),
        ("CA ", "CA"),
        (" ca ", "CA"),
        ("ny", "NY"),
    ],
)
def test_code_forms_normalize_to_uppercase(raw: str, expected: str) -> None:
    assert canonical_us_state(raw) == expected


def test_already_canonical_is_a_fixed_point() -> None:
    # canonical(canonical(x)) == canonical(x) for every known state — this is
    # what makes the backfill idempotent.
    for code in US_STATE_CODES:
        assert canonical_us_state(code) == code
        assert canonical_us_state(canonical_us_state(code)) == code


@pytest.mark.parametrize(
    "raw",
    [
        "Washington DC",
        "washington dc",
        "Washington D.C.",
        "washington, d.c.",
    ],
)
def test_district_of_columbia_spellings(raw: str) -> None:
    assert canonical_us_state(raw) == "DC"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "Ontario",  # Canadian province
        "London",  # foreign city
        "San Francisco",  # a city in the state slot
        "Puerto Rico",  # US territory — deliberately out of scope (50 + DC)
        "PR",  # territory code — out of scope
        "XX",  # not a real code
        "Californiaa",  # near-miss typo
    ],
)
def test_unknown_or_non_us_returns_none(raw: str | None) -> None:
    assert canonical_us_state(raw) is None
