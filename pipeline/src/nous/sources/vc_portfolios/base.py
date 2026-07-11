"""Common types for VC portfolio adapters.

Each adapter is responsible for one VC firm's portfolio page (or backing
JSON API). The contract is intentionally narrow: hand back a list of
``PortfolioEntry`` rows. Caller (M3 vc-portfolios pipeline stage, landing
in Chunk 6b) owns dedup, fuzzy-matching, and DB writes.

Hard-fail contract
------------------
A successful ``fetch`` returns a **non-empty** list. Every registered firm
has a public portfolio, so zero parsed entries always means the page's
structure drifted out from under the adapter (selector rot, moved JSON
island, redesign) — never "this firm has no companies". Historically some
adapters raised on a structural miss while others silently returned ``[]``,
which read as an empty portfolio and rotted unnoticed. The contract is now
uniform: a zero-yield parse raises :class:`AdapterStructuralError` (use
:func:`ensure_entries`), so the breakage lands in
``refresh-vc-portfolios``' per-firm failure isolation and trips the
``adapter-health`` canary instead of silently starving the catalog.
Transport-level failures (HTTP errors, robots blocks, timeouts) are NOT
wrapped — they propagate as their own exception types.
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


class AdapterStructuralError(RuntimeError):
    """The page fetched fine but its expected structure was missing.

    Raised when an adapter's anchor (CSS selector, JSON island, sitemap
    pattern, API response shape) matches nothing, or when a parse walks the
    page and yields zero usable entries. Distinct from transport errors
    (``httpx.HTTPStatusError``, ``RobotsBlockedError``, timeouts), which mean
    the page never arrived — this one means the site redesigned and the
    adapter needs updating.

    Subclasses ``RuntimeError`` so pre-existing ``except RuntimeError``
    handlers and tests keep working.
    """


def ensure_entries(
    entries: list[PortfolioEntry], firm: str, *, context: str
) -> list[PortfolioEntry]:
    """Enforce the hard-fail contract: zero parsed entries is a structural miss.

    Adapters call this on their final entry list (after any pagination /
    aggregation, so a legitimately-empty *page* within a multi-page walk is
    fine — only a zero *total* trips it). ``context`` names the structure
    that came up empty so the error is actionable from a log line alone.
    """
    if not entries:
        raise AdapterStructuralError(
            f"{firm}: parsed 0 portfolio entries ({context}); "
            "page structure likely changed"
        )
    return entries


class PortfolioEntry(BaseModel):
    """One company surfaced from a VC portfolio page."""

    firm: str
    name: str
    website: str | None
    description: str | None
    source_url: str


class PortfolioAdapter(Protocol):
    """Async adapter that maps a single VC's portfolio page to PortfolioEntry rows.

    ``fetch`` returns a non-empty list or raises: structural misses raise
    :class:`AdapterStructuralError` (see the module docstring's hard-fail
    contract); transport failures propagate as their own types. Callers
    (``refresh-vc-portfolios``, ``adapter-health``) isolate per-firm errors,
    so raising never takes down a whole sweep.
    """

    firm: str

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]: ...
