"""Slug and name normalization utilities.

Used for generating URL-safe slugs for companies and for
de-duplication matching on normalized company names.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata

# Suffixes to strip (case-insensitive, with optional trailing punctuation).
# Order matters: strip longer tokens before shorter ones to avoid partial matches.
_SUFFIX_PATTERN = re.compile(
    r"\s*,?\s*\b("
    r"corporations?|holdings?|incorporated|inc|llc|l\.l\.c|corp|co|ltd|lp|llp"
    r")\b\.?$",
    re.IGNORECASE,
)


def _normalize_unicode(name: str) -> str:
    """NFKD-decompose, drop combining marks, re-encode to ASCII."""
    nfkd = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _strip_suffixes(name: str) -> str:
    """Remove known corporate suffixes repeatedly until stable."""
    prev = None
    result = name.strip()
    while prev != result:
        prev = result
        result = _SUFFIX_PATTERN.sub("", result).strip()
    return result


def strip_corporate_suffix(name: str) -> str:
    """Public alias for :func:`_strip_suffixes`.

    Strips common corporate suffixes (Inc., LLC, Corp., Co., Ltd., Holdings,
    Corporation, LP, LLP, Incorporated) from *name* repeatedly until stable.
    Preserves original capitalisation and spacing.

    Examples:
        "Acme, Inc."         → "Acme"
        "Foo Bar Holdings"   → "Foo Bar"
        "Baz LLC"            → "Baz"
    """
    return _strip_suffixes(name)


def slugify(name: str) -> str:
    """Return a URL-safe slug from a company name.

    Steps:
    1. NFKD-normalize unicode (café → cafe).
    2. Strip common corporate suffixes (Inc., LLC, Corp., Co., Ltd.,
       Holdings, Corporation, LP, LLP).
    3. Lowercase.
    4. Replace non-alphanumeric runs with hyphens.
    5. Strip leading/trailing hyphens.

    Examples:
        "Acme, Inc."   → "acme"
        "Foo Bar LLC"  → "foo-bar"
        "Café Co."     → "cafe"
    """
    cleaned = _normalize_unicode(name)
    cleaned = _strip_suffixes(cleaned)
    cleaned = cleaned.lower()
    # Replace any sequence of non-alphanumeric characters with a hyphen.
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    return cleaned.strip("-")


def normalize_name(name: str) -> str:
    """Return a match key: lowercase, suffix-stripped, no whitespace or punctuation.

    Used purely as a comparison key — never shown to users.  Stripping internal
    whitespace lets "OpenAI" and "Open AI Inc" collide on the same key so the
    same real company isn't split into two rows by stylization.

    Examples:
        "Acme, Inc."   → "acme"
        "Foo Bar LLC"  → "foobar"
        "Open AI Inc"  → "openai"
        "Café Co."     → "cafe"
    """
    cleaned = _normalize_unicode(name)
    cleaned = _strip_suffixes(cleaned)
    cleaned = cleaned.lower()
    return re.sub(r"[^a-z0-9]+", "", cleaned)


def slug_with_disambiguator(base: str, cik: str | None) -> str:
    """Append a stable 6-hex-char suffix to a base slug to resolve collisions.

    If *cik* is provided, the suffix is the first 6 hex chars of sha256(cik),
    making it deterministic for the same company.  If *cik* is None or empty,
    a random 3-byte value is used (non-deterministic, acceptable for rare edge
    cases where CIK is absent).

    Example: "acme" → "acme-a3f9c2"
    """
    suffix = hashlib.sha256(cik.encode()).hexdigest()[:6] if cik else os.urandom(3).hex()
    return f"{base}-{suffix}"
