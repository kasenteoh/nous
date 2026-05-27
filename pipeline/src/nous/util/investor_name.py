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


def canonicalize_investor_name(name: str) -> str:
    """Return the normalized lookup key for an investor name.

    Lowercase, suffix-stripped, whitespace-collapsed. Empty input returns
    the empty string — callers should check before inserting.

    Examples:
        "Sequoia Capital"           -> "sequoia"
        "Lightspeed Venture Partners" -> "lightspeed"
        "Founders Fund"             -> "founders"
        "a16z"                      -> "a16z"
        "YC"                        -> "yc"
    """
    cleaned = name.strip()
    prev: str | None = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _SUFFIX_PATTERN.sub("", cleaned).strip()
    cleaned = cleaned.lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned
