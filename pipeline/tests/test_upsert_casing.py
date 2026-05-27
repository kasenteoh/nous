"""Unit tests for the cross-source casing-upgrade helper in db.upsert.

The full upgrade path (matching an existing row, then rewriting its display
name) is exercised by the DB-gated tests in test_auto_create.py. These cover
the pure decision function and always run.
"""

from __future__ import annotations

from nous.db.upsert import _is_lowercase_variant_of


def test_upgrades_lowercase_to_cased() -> None:
    assert _is_lowercase_variant_of("Airbnb", "airbnb") is True
    assert _is_lowercase_variant_of("Common Room", "common room") is True
    assert _is_lowercase_variant_of("OpenAI", "openai") is True


def test_no_upgrade_when_existing_already_cased() -> None:
    # Identical — nothing to do.
    assert _is_lowercase_variant_of("Airbnb", "Airbnb") is False
    # Never downgrade a cased name to lowercase.
    assert _is_lowercase_variant_of("airbnb", "Airbnb") is False


def test_no_upgrade_for_names_differing_beyond_case() -> None:
    assert _is_lowercase_variant_of("Airbnb", "airtable") is False
    # Whitespace difference is more than a casing difference.
    assert _is_lowercase_variant_of("Air Bnb", "airbnb") is False
