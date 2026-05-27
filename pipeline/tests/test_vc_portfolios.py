"""Tests for nous.sources.vc_portfolios.*

Each adapter is exercised against a checked-in fixture via an httpx mock
transport — no network calls. The fixture is the contract; if the VC redesigns
its page and one of these tests starts failing, that's the signal to update
the adapter (and the fixture).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios import ADAPTERS, PortfolioEntry

FIXTURES = Path(__file__).parent / "fixtures" / "vc_portfolios"
USER_AGENT = "nous-test test@example.com"


# ---------------------------------------------------------------------------
# Transport helpers (shared shape with test_homepage.py)
# ---------------------------------------------------------------------------


class _Route:
    def __init__(
        self,
        url_contains: str,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        method: str | None = None,
    ) -> None:
        self.url_contains = url_contains
        self.body = body
        self.status = status
        self.content_type = content_type
        self.method = method
        self.call_count = 0

    def matches(self, request: httpx.Request) -> bool:
        if self.method is not None and request.method.upper() != self.method.upper():
            return False
        return self.url_contains in str(request.url)


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, routes: list[_Route]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for route in self._routes:
            if route.matches(request):
                route.call_count += 1
                resp = httpx.Response(
                    route.status,
                    content=route.body,
                    headers={"content-type": route.content_type},
                )
                if route.status >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {route.status}", request=request, response=resp
                    )
                return resp
        # Unmatched -> 404 (used so robots.txt probes don't blow up the test).
        return httpx.Response(404, content=b"Not Found")


def _inject_transport(client: HomepageClient, transport: _MockTransport) -> None:
    assert client._client is not None
    assert client._robots is not None
    client._client = httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    client._robots._client = httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


def _html_routes(adapter_url: str, fixture_path: Path) -> list[_Route]:
    """Standard pair: 404 for robots (allow-all) + fixture body for the portfolio URL."""
    body = fixture_path.read_bytes()
    # Match the URL netloc — broader than full URL so query-string/path variants land.
    netloc = httpx.URL(adapter_url).host
    return [
        _Route(f"{netloc}/robots.txt", b"", status=404),
        _Route(netloc, body),
    ]


# ---------------------------------------------------------------------------
# YC adapter — needs its own routes because it does the Algolia POST dance.
# ---------------------------------------------------------------------------


_YC_PORTFOLIO_HTML = """<!DOCTYPE html>
<html><head></head><body>
<script>
window.AlgoliaOpts = {"app":"45BWZJ1SGC","key":"test-algolia-key"};
</script>
</body></html>
"""


@pytest.mark.asyncio
async def test_yc_adapter_drops_pre_seed_and_keeps_other_stages() -> None:
    algolia_body = (FIXTURES / "yc.json").read_bytes()
    routes = [
        _Route("ycombinator.com/robots.txt", b"", status=404),
        _Route("ycombinator.com/companies", _YC_PORTFOLIO_HTML.encode("utf-8")),
        _Route(
            "algolia.net",
            algolia_body,
            content_type="application/json",
            method="POST",
        ),
    ]
    transport = _MockTransport(routes)

    adapter = ADAPTERS["yc"]
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, transport)
        entries = await adapter.fetch(client)

    assert len(entries) > 50, f"YC adapter returned only {len(entries)} entries"

    names = [e.name for e in entries]
    assert "Airbnb" in names, "YC fixture's non-Pre-Seed anchor (Airbnb) missing"
    assert "Fixture Pre-Seed Co" not in names, (
        "YC adapter failed to drop the Pre-Seed entry"
    )

    for entry in entries:
        assert entry.firm == "yc"
        assert entry.source_url == adapter.PORTFOLIO_URL  # type: ignore[attr-defined]
        assert isinstance(entry, PortfolioEntry)


# ---------------------------------------------------------------------------
# HTML / JSON-island adapters — parameterised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "firm,fixture,must_contain,min_count",
    [
        ("a16z", "a16z.html", "11x", 50),
        ("sequoia", "sequoia.html", "Airbnb", 50),
        ("lightspeed", "lightspeed.html", "Anthropic", 50),
        ("founders_fund", "founders_fund.html", "Palantir", 50),
        ("greylock", "greylock.html", "Anthropic", 50),
    ],
)
@pytest.mark.asyncio
async def test_adapter_parses_fixture(
    firm: str,
    fixture: str,
    must_contain: str,
    min_count: int,
) -> None:
    adapter = ADAPTERS[firm]
    routes = _html_routes(adapter.PORTFOLIO_URL, FIXTURES / fixture)  # type: ignore[attr-defined]
    transport = _MockTransport(routes)

    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, transport)
        entries = await adapter.fetch(client)

    assert len(entries) > min_count, (
        f"{firm} adapter parsed only {len(entries)} entries from fixture {fixture}"
    )
    names = [e.name for e in entries]
    assert must_contain in names, (
        f"{firm} adapter did not surface known-good company {must_contain!r}; "
        f"first 10 names were {names[:10]!r}"
    )
    for entry in entries:
        assert entry.firm == firm
        assert entry.source_url == adapter.PORTFOLIO_URL  # type: ignore[attr-defined]
        assert isinstance(entry, PortfolioEntry)


# ---------------------------------------------------------------------------
# Khosla — landing page + nine category subpages aggregated.
# ---------------------------------------------------------------------------


_KHOSLA_CATEGORIES = (
    "consumer-and-retail",
    "digital-health",
    "enterprise",
    "exits",
    "fintech",
    "frontier",
    "med-tech-and-diagnostics",
    "sustainability",
    "therapeutics",
)


@pytest.mark.asyncio
async def test_khosla_adapter_aggregates_categories() -> None:
    adapter = ADAPTERS["khosla"]
    routes: list[_Route] = [
        _Route("khoslaventures.com/robots.txt", b"", status=404),
        _Route(
            "khoslaventures.com/portfolio",
            (FIXTURES / "khosla.html").read_bytes(),
        ),
    ]
    for cat in _KHOSLA_CATEGORIES:
        routes.append(
            _Route(
                f"khoslaventures.com/category/{cat}",
                (FIXTURES / "khosla_categories" / f"{cat}.html").read_bytes(),
            )
        )
    transport = _MockTransport(routes)

    # Bypass the 1-req/sec/domain throttle in tests — we make 10 same-domain fetches
    # and the throttle would add ~9s of dead time to every CI run.
    async with HomepageClient(
        user_agent=USER_AGENT, requests_per_second_per_domain=1000.0
    ) as client:
        _inject_transport(client, transport)
        entries = await adapter.fetch(client)

    assert len(entries) > 50, f"Khosla adapter aggregated only {len(entries)} entries"
    names = [e.name for e in entries]
    assert "OpenAI" in names
    assert len(set(names)) == len(names), "Khosla adapter must dedup across categories"
    for entry in entries:
        assert entry.firm == "khosla"
        assert entry.source_url == adapter.PORTFOLIO_URL  # type: ignore[attr-defined]
        assert isinstance(entry, PortfolioEntry)


# ---------------------------------------------------------------------------
# Lightspeed-specific: every entry should have website=None per the spec
# (resolve-homepages stage fills these in later).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lightspeed_yields_no_websites() -> None:
    adapter = ADAPTERS["lightspeed"]
    routes = _html_routes(adapter.PORTFOLIO_URL, FIXTURES / "lightspeed.html")  # type: ignore[attr-defined]
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, _MockTransport(routes))
        entries = await adapter.fetch(client)
    assert entries
    assert all(e.website is None for e in entries), (
        "Lightspeed listing cards do not include website URLs; "
        "adapter must yield website=None for every entry"
    )


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_adapter_registry_has_all_seven_firms() -> None:
    expected = {"yc", "a16z", "sequoia", "lightspeed", "founders_fund", "greylock", "khosla"}
    assert set(ADAPTERS.keys()) == expected
    for firm, adapter in ADAPTERS.items():
        assert adapter.firm == firm


# ---------------------------------------------------------------------------
# YC fixture sanity: this anchors the contract that the fixture itself contains
# both a Pre-Seed entry (for the filter to bite on) and a non-Pre-Seed entry.
# ---------------------------------------------------------------------------


def test_yc_fixture_contains_pre_seed_and_non_pre_seed_entries() -> None:
    data = json.loads((FIXTURES / "yc.json").read_text())
    hits = data["results"][0]["hits"]
    stages = {h.get("stage") for h in hits}
    assert "Pre-Seed" in stages, "YC fixture lacks a Pre-Seed entry"
    assert stages - {"Pre-Seed"}, "YC fixture lacks any non-Pre-Seed entries"
