"""Tests for hardened resolve_homepage — directory/aggregator rejection and
soft-parked page detection.

All HTTP is mocked via ResolverTransport (copied pattern from test_homepage.py).
No real network calls are made.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from nous.sources.homepage import (
    HomepageClient,
    resolve_homepage,
)
from nous.sources.reject_hosts import (
    AGGREGATOR_HOSTS,
    DIRECTORY_PATH_RE,
    is_aggregator_url,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Directory page: mentions company name but belongs to startupintros.com
HTML_DIRECTORY_PAGE = (FIXTURES / "startupintros_directory_page.html").read_text()

# Soft "for sale" page: softer signals than hard "domain is for sale"
HTML_SOFT_FOR_SALE = (FIXTURES / "soft_for_sale_page.html").read_text()

# Real company homepage: name in <title> and <h1>
HTML_REAL_COMPANY = (FIXTURES / "real_company_homepage.html").read_text()

# FrenFlow's site, which merely LISTS Kalshi as a supported venue. The
# production resolver wrongly accepted this for the company "Kalshi" because
# "Kalshi" appears in an <h1> — a strong position — even though FrenFlow, not
# Kalshi, is the subject.
HTML_FRENFLOW_LISTS_KALSHI = (FIXTURES / "frenflow_lists_kalshi.html").read_text()

# The real Kalshi homepage: Kalshi is the dominant subject of <title> + <h1>.
HTML_KALSHI_REAL = (FIXTURES / "kalshi_real_homepage.html").read_text()

USER_AGENT = "nous-test test@example.com"


# ---------------------------------------------------------------------------
# Transport helpers (same pattern as test_homepage.py)
# ---------------------------------------------------------------------------


class ResolverTransport(httpx.AsyncBaseTransport):
    """Dispatches host-keyed responses; robots.txt always 404 (allow all)."""

    def __init__(self, host_responses: dict[str, tuple[int, str]]) -> None:
        self._host_responses = host_responses

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        host = request.url.host

        if "robots.txt" in url_str:
            return httpx.Response(404, content=b"Not Found")

        if host in self._host_responses:
            status, body = self._host_responses[host]
            resp = httpx.Response(
                status,
                content=body.encode(),
                headers={"content-type": "text/html"},
            )
            if status >= 400:
                raise httpx.HTTPStatusError(
                    f"HTTP {status}", request=request, response=resp
                )
            return resp

        return httpx.Response(404, content=b"Not Found")


def _inject_transport(client: HomepageClient, transport: ResolverTransport) -> None:
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


class MockSearchHomepageClient(HomepageClient):
    """Overrides search_companies with a canned list (no real DDG call)."""

    def __init__(self, search_results: list[str], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._mock_search_results = search_results

    async def search_companies(self, query: str, limit: int = 10) -> list[str]:
        return self._mock_search_results[:limit]


# ---------------------------------------------------------------------------
# reject_hosts module — unit tests
# ---------------------------------------------------------------------------


def test_aggregator_hosts_contains_startupintros() -> None:
    """AGGREGATOR_HOSTS must include startupintros.com for directory rejection."""
    assert "startupintros.com" in AGGREGATOR_HOSTS


def test_aggregator_hosts_contains_known_directories() -> None:
    """A sampling of the required aggregator hosts must be present."""
    required = {
        "tracxn.com",
        "f6s.com",
        "crunchbase.com",
        "pitchbook.com",
        "getlatka.com",
        "theorg.com",
        "wellfound.com",
        "growjo.com",
        "similarweb.com",
        "glassdoor.com",
        "ycombinator.com",
    }
    missing = required - AGGREGATOR_HOSTS
    assert not missing, f"Missing from AGGREGATOR_HOSTS: {missing}"


def test_is_aggregator_url_exact_host() -> None:
    assert is_aggregator_url("https://startupintros.com/orgs/acme") is True


def test_is_aggregator_url_subdomain() -> None:
    """Subdomain of a blocked host must also be rejected."""
    assert is_aggregator_url("https://www.crunchbase.com/organization/acme") is True


def test_is_aggregator_url_real_homepage_not_rejected() -> None:
    assert is_aggregator_url("https://acme.com/") is False


def test_directory_path_re_matches_known_patterns() -> None:
    """DIRECTORY_PATH_RE must match /orgs/, /companies/, /company/, etc."""
    import re

    paths_to_reject = [
        "/orgs/acme",
        "/companies/acme",
        "/company/acme-corp",
        "/startups/acme",
        "/profile/acme",
    ]
    for path in paths_to_reject:
        assert re.match(DIRECTORY_PATH_RE, path), (
            f"Expected DIRECTORY_PATH_RE to match path {path!r}"
        )


def test_directory_path_re_does_not_match_product_paths() -> None:
    """Paths that are normal for company sites must not match."""
    import re

    paths_to_accept = [
        "/about",
        "/pricing",
        "/blog/companies-we-love",
        "/docs/api",
    ]
    for path in paths_to_accept:
        assert not re.match(DIRECTORY_PATH_RE, path), (
            f"DIRECTORY_PATH_RE falsely matched {path!r}"
        )


# ---------------------------------------------------------------------------
# resolve_homepage — Phase 1 TLD guesses against directory hosts
# ---------------------------------------------------------------------------


async def test_phase1_rejects_directory_host_via_aggregator_hosts() -> None:
    """slug 'acme' resolves to startupintros.com — must be REJECTED (None)."""
    # The TLD guess won't land on startupintros.com anyway, but a candidate
    # whose URL host is in AGGREGATOR_HOSTS should be rejected.  We test this
    # through Phase 2 (DDG fallback) since that is the real injection point.
    search_results = ["https://startupintros.com/orgs/acme"]
    transport = ResolverTransport(
        {"startupintros.com": (200, HTML_DIRECTORY_PAGE)}
    )

    client = MockSearchHomepageClient(
        search_results=search_results,
        user_agent=USER_AGENT,
    )
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "acme",
            "Acme",
            tlds=(),  # skip Phase 1 entirely — go straight to DDG fallback
        )

    # Even though the page mentions "Acme", the host is a known directory.
    assert result is None


async def test_phase2_rejects_directory_path_pattern() -> None:
    """DDG returns a /companies/-style path on an unknown host — must be REJECTED."""
    # Use a host that isn't in AGGREGATOR_HOSTS but whose path matches
    # DIRECTORY_PATH_RE (e.g. some aggregator we haven't listed yet).
    # The resolver should still reject it due to the path pattern.
    unknown_dir_host = "startupdb.example.com"
    search_results = [f"https://{unknown_dir_host}/companies/acme"]
    transport = ResolverTransport(
        {unknown_dir_host: (200, HTML_DIRECTORY_PAGE)}
    )

    client = MockSearchHomepageClient(
        search_results=search_results,
        user_agent=USER_AGENT,
    )
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "acme",
            "Acme",
            tlds=(),
        )

    assert result is None


async def test_phase2_rejects_soft_for_sale_page() -> None:
    """DDG candidate with 'this site is for sale' copy → REJECTED (None)."""
    search_results = ["https://foodology.com/"]
    transport = ResolverTransport(
        {"foodology.com": (200, HTML_SOFT_FOR_SALE)}
    )

    client = MockSearchHomepageClient(
        search_results=search_results,
        user_agent=USER_AGENT,
    )
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "foodology",
            "Foodology",
            tlds=(),
        )

    assert result is None


async def test_phase1_rejects_soft_for_sale_page() -> None:
    """Phase 1 TLD guess hits a soft 'for sale' page — must be REJECTED."""
    transport = ResolverTransport(
        {"foodology.com": (200, HTML_SOFT_FOR_SALE)}
    )

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "foodology",
            "Foodology",
            tlds=(".com",),
        )

    assert result is None


async def test_phase1_accepts_real_company_homepage() -> None:
    """Phase 1 TLD guess hits a real homepage with name in <title>/<h1> → ACCEPTED."""
    transport = ResolverTransport(
        {"acme.com": (200, HTML_REAL_COMPANY)}
    )

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "acme",
            "Acme",
            tlds=(".com",),
        )

    assert result is not None
    assert "acme.com" in result


async def test_phase2_accepts_real_company_homepage() -> None:
    """DDG candidate is a real company homepage → ACCEPTED."""
    search_results = ["https://acme.com/"]
    transport = ResolverTransport(
        {"acme.com": (200, HTML_REAL_COMPANY)}
    )

    client = MockSearchHomepageClient(
        search_results=search_results,
        user_agent=USER_AGENT,
    )
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "acme",
            "Acme",
            tlds=(),
        )

    assert result is not None
    assert "acme.com" in result


async def test_phase1_rejects_name_only_in_body_text_not_title_or_h1() -> None:
    """A page whose name appears only in body text (not <title> or <h1>) is REJECTED.

    The hardened resolver requires the name to appear in a strong position.
    A directory listing mentions the company name in body text — that must not
    be enough to accept the page as the company's own homepage.
    """
    # Page body mentions "acme" but title is generic and there is no <h1> with the name.
    weak_html = (
        "<html><head><title>Startup Directory</title></head>"
        "<body><p>We list companies including acme and many others.</p></body></html>"
    )
    transport = ResolverTransport({"acme.com": (200, weak_html)})

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "acme",
            "Acme",
            tlds=(".com",),
        )

    assert result is None


# ---------------------------------------------------------------------------
# Dominant-subject hardening — list-among-others / wrong-leading-brand pages
# ---------------------------------------------------------------------------


async def test_phase1_rejects_page_that_only_lists_company_among_others() -> None:
    """The Kalshi/FrenFlow incident.

    A TLD guess for "Kalshi" lands on a page whose <title> is "FrenFlow — …" and
    whose <h1> lists "Kalshi" among other venues.  "Kalshi" IS in a strong
    position (the <h1>), so the old resolver accepted it and rendered FrenFlow's
    description for Kalshi.  The hardened resolver requires Kalshi to be the
    DOMINANT subject — it is not — so the page is REJECTED.
    """
    transport = ResolverTransport({"kalshi.com": (200, HTML_FRENFLOW_LISTS_KALSHI)})

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "kalshi",
            "Kalshi",
            tlds=(".com",),
        )

    assert result is None


async def test_phase2_rejects_page_that_only_lists_company_among_others() -> None:
    """Same incident via the DDG fallback path: a candidate URL serves FrenFlow's
    multi-venue page, which must be rejected for company 'Kalshi'."""
    search_results = ["https://frenflow.com/"]
    transport = ResolverTransport({"frenflow.com": (200, HTML_FRENFLOW_LISTS_KALSHI)})

    client = MockSearchHomepageClient(
        search_results=search_results,
        user_agent=USER_AGENT,
    )
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "kalshi",
            "Kalshi",
            tlds=(),
        )

    assert result is None


async def test_phase1_accepts_real_kalshi_homepage() -> None:
    """The real Kalshi homepage (Kalshi dominant in <title> and <h1>) is ACCEPTED.

    Companion to the rejection test: proves the hardening does not over-reject a
    legitimate single-subject homepage."""
    transport = ResolverTransport({"kalshi.com": (200, HTML_KALSHI_REAL)})

    client = HomepageClient(user_agent=USER_AGENT)
    async with client:
        _inject_transport(client, transport)
        result = await resolve_homepage(
            client,
            "kalshi",
            "Kalshi",
            tlds=(".com",),
        )

    assert result is not None
    assert "kalshi.com" in result


# ---------------------------------------------------------------------------
# parked.py — soft parked-page signatures
# ---------------------------------------------------------------------------


def test_looks_parked_this_site_is_for_sale() -> None:
    """'this site is for sale' phrase must trigger parked detection."""
    from nous.sources.parked import looks_parked

    html = "<html><body><h1>This site is for sale</h1></body></html>"
    assert looks_parked(html) is True


def test_looks_parked_available_for_purchase() -> None:
    from nous.sources.parked import looks_parked

    html = "<html><body><p>Available for purchase — contact us.</p></body></html>"
    assert looks_parked(html) is True


def test_looks_parked_this_website_is_for_sale() -> None:
    from nous.sources.parked import looks_parked

    html = "<html><body><p>This website is for sale.</p></body></html>"
    assert looks_parked(html) is True


def test_looks_parked_inquire_about_this_domain() -> None:
    from nous.sources.parked import looks_parked

    html = "<html><body><p>Inquire about this domain today.</p></body></html>"
    assert looks_parked(html) is True


def test_looks_parked_soft_for_sale_fixture() -> None:
    """The full soft-for-sale fixture page must be detected as parked."""
    from nous.sources.parked import looks_parked

    assert looks_parked(HTML_SOFT_FOR_SALE) is True


def test_real_homepage_not_parked_regression() -> None:
    """Real company homepage must NOT be flagged as parked."""
    from nous.sources.parked import looks_parked

    assert looks_parked(HTML_REAL_COMPANY) is False
