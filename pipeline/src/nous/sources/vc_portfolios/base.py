"""Common types for VC portfolio adapters.

Each adapter is responsible for one VC firm's portfolio page (or backing
JSON API). The contract is intentionally narrow: hand back a list of
``PortfolioEntry`` rows. Caller (M3 vc-portfolios pipeline stage, landing
in Chunk 6b) owns dedup, fuzzy-matching, and DB writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from nous.sources.homepage import HomepageClient


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
