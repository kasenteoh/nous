"""Pure unit tests for nous.util.investor_name helpers.

No database or network access — pure string-classification logic, so these run
everywhere (including CI without DATABASE_URL). The DB-level behaviour (purge +
angel classification through run_dedup_investors, junk rejection through
upsert_investor) is covered in test_investor_dedup.py.

Coverage:
- canonicalize_investor_name: a16z↔Andreessen Horowitz alias merges; a16z Crypto
  stays distinct.
- is_junk_investor_name: placeholder names are junk; real firms are not.
- is_individual_investor_name: human names → True; firms (incl. surname-pair
  firms like "Andreessen Horowitz") → False.
"""

from __future__ import annotations

import pytest

from nous.util.investor_name import (
    canonicalize_investor_name,
    is_individual_investor_name,
    is_junk_investor_name,
)

# ---------------------------------------------------------------------------
# canonicalize_investor_name — a16z alias + a16z Crypto distinctness
# ---------------------------------------------------------------------------


def test_a16z_aliases_to_andreessen_horowitz() -> None:
    """The two names for the same firm resolve to one canonical key."""
    assert canonicalize_investor_name("a16z") == canonicalize_investor_name(
        "Andreessen Horowitz"
    )


@pytest.mark.parametrize("variant", ["a16z", "A16Z", "a16Z", "  a16z  "])
def test_a16z_casing_and_whitespace_variants_merge(variant: str) -> None:
    """Casing/whitespace variants of a16z all reach the Andreessen canonical."""
    assert canonicalize_investor_name(variant) == canonicalize_investor_name(
        "Andreessen Horowitz"
    )


@pytest.mark.parametrize("crypto", ["a16z Crypto", "a16z crypto", "A16Z Crypto"])
def test_a16z_crypto_is_distinct_from_a16z(crypto: str) -> None:
    """a16z Crypto is a genuinely separate fund and must NOT merge into a16z."""
    assert canonicalize_investor_name(crypto) != canonicalize_investor_name("a16z")
    assert canonicalize_investor_name(crypto) != canonicalize_investor_name(
        "Andreessen Horowitz"
    )


def test_canonical_suffix_stripping_unaffected() -> None:
    """Sanity: the alias additions don't disturb ordinary suffix stripping."""
    assert canonicalize_investor_name("Sequoia Capital") == "sequoia"
    assert canonicalize_investor_name("Lightspeed Venture Partners") == "lightspeed"


# ---------------------------------------------------------------------------
# is_junk_investor_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # The live offenders called out in the bug report.
        "a group of investors",
        "A group of investors",
        "undisclosed",
        "Undisclosed",
        "investors",
        "Investors",
        "angel investors",
        "Angel Investors",
        "strategic investors",
        "existing investors",
        "various",
        "n/a",
        "N/A",
        # Obvious variants.
        "group of investors",
        "a consortium of investors",
        "a syndicate of investors",
        "undisclosed investors",
        "unnamed investors",
        "Various Investors",
        "other investors",
        "additional investors",
        "individual investors",
        "institutional investors",
        "private investors",
        "a number of strategic investors",
        "several investors",
        "multiple angel investors",
        "the public",
        "self-funded",
        "bootstrapped",
        "none",
        "unknown",
        "anonymous",
        "confidential",
        "tbd",
        "et al.",
        # Punctuation / empty-ish.
        "-",
        "--",
        "?",
        "   ",
        "",
    ],
)
def test_junk_names_are_flagged(name: str) -> None:
    assert is_junk_investor_name(name), f"{name!r} should be junk"


@pytest.mark.parametrize(
    "name",
    [
        # Real institutional firms — must never be flagged as junk.
        "Sequoia Capital",
        "Andreessen Horowitz",
        "a16z",
        "a16z Crypto",
        "Founders Fund",  # contains "fund" but is a real firm
        "New Enterprise Associates",  # contains "enterprise"/"associates"
        "Group 11",  # contains "group" but is a real firm
        "Angel Investors Network LLC",  # "angel"/"investors" but a proper firm
        "Tiger Global",
        "Y Combinator",
        "Kleiner Perkins",
        "GV",
        "8VC",
        "Insight Partners",
        # Real individuals — not junk (they're real investors, just angels).
        "Jeff Bezos",
        "Elon Musk",
        "Reid Hoffman",
    ],
)
def test_real_names_are_not_junk(name: str) -> None:
    assert not is_junk_investor_name(name), f"{name!r} should NOT be junk"


# ---------------------------------------------------------------------------
# is_individual_investor_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Jeff Bezos",
        "Elon Musk",
        "Marc Andreessen",
        "Reid Hoffman",
        "Peter Thiel",
        "Marc Benioff",
        "Eric Schmidt",
        "Mark Cuban",
        "Max Levchin",
        "Vinod Khosla",
        "Sam Altman",
        "Drew Houston",
        "John A. Doe",  # three tokens with a middle initial
        "Mary Meeker",
    ],
)
def test_individuals_are_classified(name: str) -> None:
    assert is_individual_investor_name(name), f"{name!r} should be an individual"


@pytest.mark.parametrize(
    "name",
    [
        # Surname-pair firms — the exact trap the given-name gate guards against.
        "Andreessen Horowitz",
        "Kleiner Perkins",
        "General Catalyst",
        "Tiger Global",
        "Draper Fisher",
        "Social Capital",
        # Firms with explicit firm-marker tokens.
        "Sequoia Capital",
        "Founders Fund",
        "Battery Ventures",
        "Index Ventures",
        "Lightspeed Venture Partners",
        "Khosla Ventures",
        "Bessemer Venture Partners",
        "Thrive Capital",
        "Insight Partners",
        "New Enterprise Associates",
        # Short/abbreviation/symbol firms.
        "GV",
        "8VC",
        "Coatue",
        "Benchmark",
        "Group 11",
        # Junk is never an individual.
        "a group of investors",
        "undisclosed",
        "angel investors",
    ],
)
def test_firms_and_junk_are_not_individuals(name: str) -> None:
    assert not is_individual_investor_name(name), (
        f"{name!r} should NOT be an individual"
    )


def test_known_firm_override_blocks_individual_classification() -> None:
    """A name that *looks* individual is never an angel when known_firm=True."""
    # "Marc Andreessen" reads as a person, but if the registry says it's a firm
    # (defensive belt-and-suspenders), the override wins.
    assert is_individual_investor_name("Marc Andreessen") is True
    assert is_individual_investor_name("Marc Andreessen", known_firm=True) is False


def test_unknown_first_name_is_not_classified() -> None:
    """A plausible human name whose first token isn't a recognized given name
    stays unclassified — the conservative miss (left 'unknown'), never a firm
    mislabel."""
    # "Xyzzy" is not in the given-names set, so we don't claim it's an angel.
    assert is_individual_investor_name("Xyzzy Plugh") is False


def test_single_token_name_is_not_individual() -> None:
    """One-token names are too ambiguous to call an individual."""
    assert is_individual_investor_name("Jeff") is False
    assert is_individual_investor_name("Bezos") is False
