"""Async SEC EDGAR client for fetching Form D filings.

Uses the EDGAR full-text search endpoint to page through Form D results
and downloads the primary_doc.xml for each filing.

Rate limit: capped at ``requests_per_second`` (default 5.0), comfortably
below SEC's stated 10 req/s ceiling.

Every request includes a User-Agent header — SEC blocks anonymous traffic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)


class FilingHit(BaseModel):
    """A single result from the EDGAR search-index."""

    accession_number: str  # dashed form, e.g. "0001234567-25-000001"
    cik: str  # zero-padded 10 digits, e.g. "0001234567"
    entity_name: str
    filing_date: date


def _is_retryable(exc: BaseException) -> bool:
    """Return True for errors that warrant a retry."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.RequestError)


class EdgarClient:
    """Async context-manager that wraps EDGAR HTTP calls with rate-limiting and retries."""

    # Full-text search endpoint (returns JSON hits)
    BASE_SEARCH = "https://efts.sec.gov/LATEST/search-index"
    # Primary filing archive root
    BASE_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

    def __init__(
        self,
        user_agent: str,
        requests_per_second: float = 5.0,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise ValueError(
                "user_agent must be a non-empty string containing a contact email. "
                "SEC EDGAR blocks anonymous traffic — this is non-negotiable."
            )
        self._user_agent = user_agent
        self._rps = requests_per_second
        # _min_interval enforces the rate limit: each request waits until at
        # least this many seconds have elapsed since the previous one.
        self._min_interval: float = 1.0 / requests_per_second
        self._last_request_at: float = 0.0
        self._rate_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> EdgarClient:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self._user_agent},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _assert_open(self) -> httpx.AsyncClient:
        """Return the underlying httpx client, raising if not inside ``async with``."""
        if self._client is None:
            raise RuntimeError("EdgarClient must be used as an async context manager.")
        return self._client

    async def _throttled_get(self, url: str, **kwargs: Any) -> httpx.Response:
        """Rate-limited GET. Serialises requests through _rate_lock so that
        concurrent callers each wait their turn before firing."""
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            wait = self._min_interval - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            client = self._assert_open()
            resp = await client.get(url, **kwargs)
            self._last_request_at = time.monotonic()
            resp.raise_for_status()
            return resp

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET with tenacity retries on 429 / 5xx / network errors."""
        return await self._throttled_get(url, **kwargs)

    async def search_form_d(
        self, start: date, end: date
    ) -> AsyncIterator[FilingHit]:
        """Yield every Form D filing submitted between *start* and *end* (inclusive).

        Pages through the search-index in chunks of 100 until fewer than 100
        results are returned (indicating the last page).
        """
        page_size = 100
        offset = 0
        while True:
            params: dict[str, str | int] = {
                "forms": "D",
                "dateRange": "custom",
                "startdt": start.isoformat(),
                "enddt": end.isoformat(),
                "from": offset,
                "size": page_size,
            }
            resp = await self._get(self.BASE_SEARCH, params=params)
            data = resp.json()
            hits: list[dict[str, Any]] = data["hits"]["hits"]
            for hit in hits:
                filing_hit = _parse_hit(hit)
                if filing_hit is not None:
                    yield filing_hit
            if len(hits) < page_size:
                break
            offset += page_size

    async def fetch_primary_doc(self, cik: str, accession_number: str) -> str:
        """Return the primary_doc.xml body for a single filing as a string.

        Args:
            cik: Zero-padded 10-digit CIK, e.g. ``"0001234567"``.
            accession_number: Dashed accession, e.g. ``"0001234567-25-000001"``.

        Returns:
            Raw XML text.
        """
        # Strip leading zeros from CIK for the URL path segment.
        cik_int = str(int(cik))
        # Archive directory uses the *un-dashed* accession number.
        accession_nodash = accession_number.replace("-", "")
        url = f"{self.BASE_ARCHIVES}/{cik_int}/{accession_nodash}/primary_doc.xml"
        resp = await self._get(url)
        return resp.text


def _parse_hit(hit: dict[str, Any]) -> FilingHit | None:
    """Convert a raw search-index hit dict into a FilingHit.

    The ``_id`` field has the form ``"0001234567-25-000001:primary_doc.xml"``.
    ``_source.ciks`` is a list; we take the first element.
    ``_source.display_names`` is a list of strings like
    ``"Acme Corp  (CIK 0001234567)"``.

    Returns None if the hit is missing required fields (e.g. no CIK).
    """
    source: dict[str, Any] = hit.get("_source", {})

    ciks: list[str] = source.get("ciks", [])
    if not ciks:
        return None
    cik = ciks[0]

    # accession_number is encoded in _id before the colon
    raw_id: str = hit.get("_id", "")
    accession_number = raw_id.split(":")[0] if ":" in raw_id else raw_id

    display_names: list[str] = source.get("display_names", [])
    # Strip the "(CIK XXXXXXXXXX)" suffix that EDGAR appends to display names.
    entity_name = display_names[0].split("  (CIK")[0].strip() if display_names else ""

    file_date_str: str = source.get("file_date", "")
    try:
        filing_date = date.fromisoformat(file_date_str)
    except (ValueError, TypeError):
        return None

    return FilingHit(
        accession_number=accession_number,
        cik=cik,
        entity_name=entity_name,
        filing_date=filing_date,
    )
