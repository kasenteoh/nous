"""Pure-unit tests for the name-quality decision helpers (no Postgres).

These pin the conservatism contract of the CASING-ONLY rename, and run in CI
without a database (the DB-gated stage behavior lives in
test_name_quality_stage.py). A candidate sourced from the stored homepage title
upgrades degenerate casing ("docusign" -> "DocuSign", "AIRBNB" -> "Airbnb") but
never swaps in a different word and never downgrades a properly-cased name.
"""

from __future__ import annotations

from nous.pipeline.name_quality import (
    _better_casing,
    _candidate_from_content,
    _is_degenerate_casing,
)


def test_candidate_from_prepended_title_line() -> None:
    """extract_visible_text prepends the <title> as the first line; the brand is
    the leading segment before the first separator."""
    content = "DocuSign | The #1 way to send and sign\nSend and sign documents."
    assert _candidate_from_content(content) == "DocuSign"


def test_candidate_from_bare_title_and_boilerplate() -> None:
    # A bare title with no separator is taken whole.
    assert _candidate_from_content("DocuSign\nbody text") == "DocuSign"
    # "Welcome to" / "Home" boilerplate is stripped to the brand.
    assert (
        _candidate_from_content("Welcome to DocuSign — eSignature\nbody")
        == "DocuSign"
    )


def test_candidate_strips_corporate_suffix() -> None:
    assert _candidate_from_content("DocuSign, Inc. | eSignature\n...") == "DocuSign"


def test_candidate_none_on_empty_content() -> None:
    assert _candidate_from_content("") is None
    assert _candidate_from_content("\n\n   \n") is None


def test_is_degenerate_casing() -> None:
    assert _is_degenerate_casing("docusign") is True  # all-lower
    assert _is_degenerate_casing("AIRBNB") is True  # all-upper
    assert _is_degenerate_casing("DocuSign") is False  # mixed
    assert _is_degenerate_casing("Docusign") is False  # mixed (capital D)
    assert _is_degenerate_casing("123-4") is False  # no cased letters


def test_better_casing_upgrades_degenerate() -> None:
    assert _better_casing("DocuSign", "docusign") == "DocuSign"  # lower -> mixed
    assert _better_casing("Airbnb", "AIRBNB") == "Airbnb"  # upper -> mixed
    # The homepage knows its own internal capital S.
    assert _better_casing("DocuSign", "Docusign") == "DocuSign"


def test_better_casing_rejects_noop_and_different_word() -> None:
    # Identical — nothing to do.
    assert _better_casing("DocuSign", "DocuSign") is None
    # A genuinely different word is not a casing variant.
    assert _better_casing("Globex", "Acme") is None
    assert _better_casing("Airtable", "Airbnb") is None


def test_better_casing_never_downgrades() -> None:
    # A flat-cased homepage title must not flatten a properly-cased name.
    assert _better_casing("docusign", "DocuSign") is None
    assert _better_casing("DOCUSIGN", "DocuSign") is None
