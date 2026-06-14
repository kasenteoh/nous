"""Investor name canonicalization.

Without normalization, "Sequoia", "Sequoia Capital", and "SEQUOIA CAPITAL"
would each be a separate investor row. We strip common firm-suffix words and
lowercase to derive a stable lookup key.

The display name (preserved separately on `investors.name`) keeps the
original casing as the LLM extracted it.
"""

from __future__ import annotations

import re

# Trailing words to strip when computing the normalized key. Matched
# case-insensitively at the end of the name (with optional preceding
# whitespace). Repeat-stripped to handle e.g. "Acme Partners LP".
_SUFFIX_PATTERN = re.compile(
    r"\s+\b(capital|ventures?|partners?|management|group|fund|lp|llc)\b\.?$",
    re.IGNORECASE,
)

# Conservative alias map: only unambiguous, true-duplicate firm names.
#
# Rationale for conservatism: we only map names that refer to the *exact same
# legal entity* and where there is no reasonable ambiguity. "a16z" and
# "Andreessen Horowitz" are the same firm; "GV" and "Google Ventures" are
# the same firm. By contrast, named sub-funds of the same family (e.g.
# "Valor Equity Partners" vs "Valor Atreides AI Fund") are DIFFERENT entities
# and are explicitly left un-aliased. When in doubt, leave it out — merging
# distinct entities is far worse than leaving mild duplicates in the DB.
#
# Keys and values are the POST-suffix-stripped, lowercased canonical forms.
# All entries are bidirectional (both sides map to the canonical "winner").
_ALIAS_PAIRS: list[tuple[str, str]] = [
    # a16z ↔ Andreessen Horowitz (same firm, commonly known by both names)
    ("a16z", "andreessen horowitz"),
    # GV (formerly Google Ventures) ↔ Google Ventures
    ("gv", "google"),
    # New Enterprise Associates ↔ NEA
    ("nea", "new enterprise associates"),
    # General Atlantic — GA is a common abbreviation
    ("ga", "general atlantic"),
    # Institutional Venture Partners ↔ IVP
    ("ivp", "institutional venture"),
    # Battery Ventures ↔ Battery
    ("battery", "battery"),
]

# Build a flat alias map: each key maps to the canonical form (the
# alphabetically-first member of the pair, for determinism).
_ALIAS_MAP: dict[str, str] = {}
for _a, _b in _ALIAS_PAIRS:
    _canonical = min(_a, _b)
    _ALIAS_MAP[_a] = _canonical
    _ALIAS_MAP[_b] = _canonical


def canonicalize_investor_name(name: str) -> str:
    """Return the normalized lookup key for an investor name.

    Lowercase, suffix-stripped, whitespace-collapsed. Alias-mapped so
    common variant names (e.g. "a16z" and "Andreessen Horowitz") resolve to
    the same canonical key. Empty input returns the empty string — callers
    should check before inserting.

    Examples:
        "Sequoia Capital"             -> "sequoia"
        "Lightspeed Venture Partners" -> "lightspeed"
        "Founders Fund"               -> "founders"
        "a16z"                        -> "a16z"
        "Andreessen Horowitz"         -> "a16z"
        "GV"                          -> "google"
        "Google Ventures"             -> "google"
        "YC"                          -> "yc"
    """
    cleaned = name.strip()
    prev: str | None = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _SUFFIX_PATTERN.sub("", cleaned).strip()
    cleaned = cleaned.lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Apply alias map — resolve to the canonical name for this equivalence class.
    return _ALIAS_MAP.get(cleaned, cleaned)
