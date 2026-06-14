"""Common types for VC portfolio adapters.

Each adapter is responsible for one VC firm's portfolio page (or backing
JSON API). The contract is intentionally narrow: hand back a list of
``PortfolioEntry`` rows. Caller (M3 vc-portfolios pipeline stage, landing
in Chunk 6b) owns dedup, fuzzy-matching, and DB writes.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from nous.sources.homepage import HomepageClient

# Regex that matches bracketed placeholders like "[untitled]", "[name]", "[TBD]".
# The a16z portfolio page uses "[untitled]" as a literal title for stealth-mode
# companies whose real name is intentionally withheld. Storing that verbatim
# name corrupts the catalog — entries like "[untitled]" collide on their
# normalized form ("untitled") and render as garbage on the site.
_BRACKETED_NAME_RE = re.compile(r"^\[.*\]$")

# Additional literal placeholder values (case-insensitive).
# Note: "untitled" is intentionally absent — "Untitled" is a legitimate company
# name (e.g. untitled.stream).  The bracketed form "[untitled]" is caught by
# _BRACKETED_NAME_RE above.  Only add bare words here that are NEVER used as
# real company names in the startup ecosystem.
_PLACEHOLDER_NAMES: frozenset[str] = frozenset(
    {
        "tbd",
        "to be determined",
        "unknown",
        "n/a",
        "placeholder",
        "company name",
        "stealth",
        "stealth startup",
        "stealth mode",
    }
)


def is_placeholder_name(name: str) -> bool:
    """Return True when *name* is empty, bracketed, or a known placeholder.

    Adapters must call this and SKIP entries that return True.  The rule:

    - Empty or whitespace-only → placeholder.
    - Matches ``^\\[.*\\]$`` (brackets surround the entire value) → placeholder.
      e.g. "[untitled]", "[TBD]", "[stealth]".
    - Lower-cased value is in :data:`_PLACEHOLDER_NAMES` → placeholder.

    This keeps bracketed stealth entries out of the catalog without touching
    valid names that merely *contain* brackets (e.g. "Acme [NY]" — rare but
    possible future case; the whole-string regex doesn't match it).
    """
    stripped = name.strip()
    if not stripped:
        return True
    if _BRACKETED_NAME_RE.match(stripped):
        return True
    return stripped.lower() in _PLACEHOLDER_NAMES


class PortfolioEntry(BaseModel):
    """One company surfaced from a VC portfolio page."""

    firm: str
    name: str
    website: str | None
    description: str | None
    source_url: str


class PortfolioAdapter(Protocol):
    """Async adapter that maps a single VC's portfolio page to PortfolioEntry rows."""

    firm: str

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]: ...
