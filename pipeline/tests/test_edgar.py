"""Tests for the EDGAR async client (nous.sources.edgar).

Uses httpx.MockTransport so no real network calls are made.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from nous.sources.edgar import EdgarClient, FilingHit

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def _load_search_sample() -> dict[str, Any]:
    return json.loads((FIXTURES / "edgar_search_sample.json").read_text())


def _load_form_d_xml() -> str:
    return (FIXTURES / "form_d_sample.xml").read_text()


class MockRoute:
    """A single (url-prefix, response) pair used by RecordingTransport."""

    def __init__(
        self,
        url_contains: str,
        *,
        status: int = 200,
        json_body: Any = None,
        text_body: str = "",
    ) -> None:
        self.url_contains = url_contains
        self.status = status
        self.json_body = json_body
        self.text_body = text_body
        self.seen_requests: list[httpx.Request] = []

    def matches(self, request: httpx.Request) -> bool:
        return self.url_contains in str(request.url)

    def build_response(self, request: httpx.Request) -> httpx.Response:
        self.seen_requests.append(request)
        if self.json_body is not None:
            content = json.dumps(self.json_body).encode()
            headers = {"content-type": "application/json"}
        else:
            content = self.text_body.encode()
            headers = {"content-type": "text/xml"}
        return httpx.Response(self.status, content=content, headers=headers)


class RouterTransport(httpx.AsyncBaseTransport):
    """Dispatches to the first matching MockRoute; raises if none match."""

    def __init__(self, routes: list[MockRoute]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for route in self._routes:
            if route.matches(request):
                resp = route.build_response(request)
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=request,
                        response=resp,
                    )
                return resp
        raise AssertionError(f"No route matched URL: {request.url}")


def _make_client(
    routes: list[MockRoute],
    user_agent: str = "test-agent test@example.com",
) -> EdgarClient:
    """Create an EdgarClient wired to a mock transport."""
    client = EdgarClient(user_agent=user_agent)
    # Patch in a pre-built httpx.AsyncClient with the mock transport.
    client._client = httpx.AsyncClient(
        transport=RouterTransport(routes),
        headers={"User-Agent": user_agent},
    )
    return client


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_empty_user_agent_raises() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        EdgarClient(user_agent="")


def test_whitespace_user_agent_raises() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        EdgarClient(user_agent="   ")


# ---------------------------------------------------------------------------
# User-Agent header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_agent_sent_on_search() -> None:
    sample = _load_search_sample()
    # Return a response with <100 hits to stop pagination after one page.
    small_sample: dict[str, Any] = dict(sample)
    small_hits = sample["hits"]["hits"][:5]
    small_sample["hits"] = {**sample["hits"], "hits": small_hits}

    route = MockRoute("efts.sec.gov", json_body=small_sample)
    client = _make_client([route], user_agent="nous-project test@example.com")

    # Consume the iterator (we don't care about hit values here, just that requests fired).
    async for _ in client.search_form_d(date(2026, 5, 1), date(2026, 5, 2)):
        pass

    assert len(route.seen_requests) >= 1
    for req in route.seen_requests:
        assert req.headers["User-Agent"] == "nous-project test@example.com"


@pytest.mark.asyncio
async def test_user_agent_sent_on_fetch_primary_doc() -> None:
    xml = _load_form_d_xml()
    route = MockRoute("sec.gov/Archives", text_body=xml)
    client = _make_client([route], user_agent="nous-project test@example.com")

    await client.fetch_primary_doc("0001858523", "0001858523-26-000003")

    assert len(route.seen_requests) == 1
    assert route.seen_requests[0].headers["User-Agent"] == "nous-project test@example.com"


# ---------------------------------------------------------------------------
# URL construction for fetch_primary_doc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_primary_doc_url_construction() -> None:
    """Verify CIK zero-stripping and accession de-dashing in the archive URL."""
    xml = _load_form_d_xml()
    route = MockRoute("sec.gov/Archives", text_body=xml)
    client = _make_client([route])

    await client.fetch_primary_doc("0001234567", "0001234567-25-000001")

    req = route.seen_requests[0]
    url = str(req.url)
    # CIK should have leading zeros stripped: 1234567
    assert "/data/1234567/" in url
    # Accession should be de-dashed: 000123456725000001
    assert "000123456725000001/primary_doc.xml" in url


@pytest.mark.asyncio
async def test_fetch_primary_doc_persefoni_url() -> None:
    """Spot-check the Persefoni AI accession matches the real fixture URL."""
    xml = _load_form_d_xml()
    route = MockRoute("sec.gov/Archives", text_body=xml)
    client = _make_client([route])

    result = await client.fetch_primary_doc("0001858523", "0001858523-26-000003")

    req = route.seen_requests[0]
    url = str(req.url)
    assert "/data/1858523/" in url
    assert "000185852326000003/primary_doc.xml" in url
    assert result == xml


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _make_page(n: int, start_idx: int = 0) -> dict[str, Any]:
    """Build a fake search response with *n* hits."""
    hits = []
    for i in range(n):
        idx = start_idx + i
        hits.append(
            {
                "_id": f"0001000{idx:03d}-26-{idx:06d}:primary_doc.xml",
                "_source": {
                    "ciks": [f"0001000{idx:03d}"],
                    "display_names": [f"Company {idx}  (CIK 0001000{idx:03d})"],
                    "file_date": "2026-05-01",
                },
            }
        )
    return {
        "hits": {
            "total": {"value": 150},
            "hits": hits,
        }
    }


class PaginationTransport(httpx.AsyncBaseTransport):
    """Returns page-1 (100 hits) then page-2 (<100 hits) based on 'from' param."""

    def __init__(self) -> None:
        self.request_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        from_param = int(request.url.params.get("from", "0"))
        page = _make_page(100, start_idx=0) if from_param == 0 else _make_page(50, start_idx=100)
        content = json.dumps(page).encode()
        return httpx.Response(200, content=content, headers={"content-type": "application/json"})


@pytest.mark.asyncio
async def test_pagination_stops_when_fewer_than_100_hits() -> None:
    transport = PaginationTransport()
    client = EdgarClient(user_agent="test test@example.com")
    client._client = httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": "test test@example.com"},
    )

    results = [h async for h in client.search_form_d(date(2026, 5, 1), date(2026, 5, 2))]

    # Two pages: 100 + 50 = 150 results
    assert len(results) == 150
    # Exactly two HTTP requests made
    assert transport.request_count == 2


@pytest.mark.asyncio
async def test_pagination_stops_exactly_at_100() -> None:
    """If first page returns exactly 100 and second returns 0, we stop after 2 requests."""

    class TwoPageTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.count = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.count += 1
            from_param = int(request.url.params.get("from", "0"))
            page = _make_page(100 if from_param == 0 else 0, start_idx=from_param)
            content = json.dumps(page).encode()
            return httpx.Response(
                200, content=content, headers={"content-type": "application/json"}
            )

    transport = TwoPageTransport()
    client = EdgarClient(user_agent="test test@example.com")
    client._client = httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": "test test@example.com"},
    )

    results = [h async for h in client.search_form_d(date(2026, 5, 1), date(2026, 5, 2))]
    assert len(results) == 100
    assert transport.count == 2


# ---------------------------------------------------------------------------
# Retry on 429
# ---------------------------------------------------------------------------


class RetryTransport(httpx.AsyncBaseTransport):
    """Returns 429 on the first call then 200 on subsequent calls."""

    def __init__(self, text_body: str) -> None:
        self._text = text_body
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        if self.call_count == 1:
            resp = httpx.Response(429, content=b"Too Many Requests")
            raise httpx.HTTPStatusError("429", request=request, response=resp)
        return httpx.Response(
            200,
            content=self._text.encode(),
            headers={"content-type": "text/xml"},
        )


@pytest.mark.asyncio
async def test_retry_on_429_succeeds() -> None:
    xml = _load_form_d_xml()
    transport = RetryTransport(xml)
    client = EdgarClient(user_agent="test test@example.com")
    client._client = httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": "test test@example.com"},
    )

    result = await client.fetch_primary_doc("0001858523", "0001858523-26-000003")

    assert result == xml
    # First attempt failed (429), second succeeded — total 2 calls.
    assert transport.call_count == 2


# ---------------------------------------------------------------------------
# Real search fixture round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_replay_with_fixture() -> None:
    """Replay the saved edgar_search_sample.json; check first result shape."""
    sample = _load_search_sample()
    # Truncate to a few hits to stop pagination on first page.
    small_hits = sample["hits"]["hits"][:3]
    small_sample: dict[str, Any] = {
        "hits": {**sample["hits"], "hits": small_hits}
    }

    route = MockRoute("efts.sec.gov", json_body=small_sample)
    client = _make_client([route])

    results: list[FilingHit] = [
        h async for h in client.search_form_d(date(2026, 5, 1), date(2026, 5, 2))
    ]

    assert len(results) == 3
    hit = results[0]
    assert hit.cik  # non-empty
    assert hit.accession_number  # non-empty
    assert hit.filing_date == date(2026, 5, 1)


# ---------------------------------------------------------------------------
# Context manager required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raises_without_context_manager() -> None:
    client = EdgarClient(user_agent="test test@example.com")
    # _client is None because we never entered __aenter__
    with pytest.raises(RuntimeError, match="context manager"):
        await client.fetch_primary_doc("0001858523", "0001858523-26-000003")
