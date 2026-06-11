"""Unit tests for the employee-count source clients + the range parser.

Each client is exercised against canned HTTP responses via an httpx mock
transport (no network). These assert the parse logic and the graceful-None
contract; real-world hit rate (Wellfound/GrowJo are bot-hostile) is out of scope.
"""

from __future__ import annotations

import httpx
import pytest

from nous.sources import careers_jobs, github_org, growjo, theorg, wellfound
from nous.sources.homepage import HomepageClient
from nous.util.employee_range import parse_employee_range

USER_AGENT = "nous-test test@example.com"


# ---------------------------------------------------------------------------
# Mock transport (same shape as test_vc_portfolios / test_homepage), plus
# per-route response headers so the GitHub Link-pagination path is testable.
# ---------------------------------------------------------------------------


class _Route:
    def __init__(
        self,
        url_contains: str,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url_contains = url_contains
        self.body = body
        self.status = status
        self.content_type = content_type
        self.headers = headers or {}

    def matches(self, request: httpx.Request) -> bool:
        return self.url_contains in str(request.url)


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, routes: list[_Route]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for route in self._routes:
            if route.matches(request):
                headers = {"content-type": route.content_type, **route.headers}
                resp = httpx.Response(route.status, content=route.body, headers=headers)
                if route.status >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {route.status}", request=request, response=resp
                    )
                return resp
        return httpx.Response(404, content=b"Not Found")


def _inject_transport(client: HomepageClient, transport: _MockTransport) -> None:
    assert client._client is not None
    assert client._robots is not None
    client._client = httpx.AsyncClient(
        transport=transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )
    client._robots._client = httpx.AsyncClient(
        transport=transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )


def _html_routes(host: str, body: bytes) -> list[_Route]:
    return [
        _Route(f"{host}/robots.txt", b"", status=404),
        _Route(host, body),
    ]


# ---------------------------------------------------------------------------
# parse_employee_range
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("11-50", (11, 50)),
        ("11–50", (11, 50)),  # en-dash
        ("11 to 50", (11, 50)),
        ("1,001-5,000", (1001, 5000)),
        ("5000+", (5000, 100_000)),
        ("250", (250, 250)),
        ("1,001 employees", (1001, 1001)),
        ("50-11", (11, 50)),  # reversed -> normalized
        ("Company size: 11-50 employees", (11, 50)),
        ("junk", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_employee_range(text: str | None, expected: tuple[int, int] | None) -> None:
    assert parse_employee_range(text) == expected


# ---------------------------------------------------------------------------
# theorg — "employeeRange":"200-500" in the embedded Next.js data.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_theorg_parses_employee_range() -> None:
    body = b'<html><body><script>{"slug":"ramp","employeeRange":"200-500"}</script></body></html>'
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, _MockTransport(_html_routes("theorg.com", body)))
        assert await theorg.get_employee_range(client, "Ramp") == (200, 500)


@pytest.mark.asyncio
async def test_theorg_returns_none_without_field() -> None:
    body = b"<html><body>no employee data here</body></html>"
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, _MockTransport(_html_routes("theorg.com", body)))
        assert await theorg.get_employee_range(client, "Ramp") is None


@pytest.mark.asyncio
async def test_theorg_returns_none_on_404() -> None:
    # No matching route -> 404 -> fetch raises -> client swallows -> None.
    routes = [_Route("theorg.com/robots.txt", b"", status=404)]
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, _MockTransport(routes))
        assert await theorg.get_employee_range(client, "Nonexistent Co") is None


# ---------------------------------------------------------------------------
# wellfound — "Company size" band.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wellfound_parses_company_size() -> None:
    body = b"<html><body><div>Company size</div><div>11-50 employees</div></body></html>"
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, _MockTransport(_html_routes("wellfound.com", body)))
        assert await wellfound.get_employee_range(client, "Acme") == (11, 50)


@pytest.mark.asyncio
async def test_wellfound_returns_none_when_blocked() -> None:
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(
            client,
            _MockTransport(
                [
                    _Route("wellfound.com/robots.txt", b"", status=404),
                    _Route("wellfound.com", b"blocked", status=403),
                ]
            ),
        )
        assert await wellfound.get_employee_range(client, "Acme") is None


# ---------------------------------------------------------------------------
# growjo — "Number of Employees" / "N employees".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_growjo_parses_employee_count() -> None:
    body = b"<html><body><p>Notion has 1,001 employees and growing</p></body></html>"
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, _MockTransport(_html_routes("growjo.com", body)))
        assert await growjo.get_employee_range(client, "Notion") == (1001, 1001)


# ---------------------------------------------------------------------------
# careers_jobs — count ATS job-listing elements, bucket the count.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_careers_jobs_counts_greenhouse_openings() -> None:
    openings = b"".join(b'<div class="opening"><a>Role</a></div>' for _ in range(15))
    body = b"<html><body><div id='grnhse_app'>" + openings + b"</div></body></html>"
    async with HomepageClient(
        user_agent=USER_AGENT, requests_per_second_per_domain=1000.0
    ) as client:
        _inject_transport(client, _MockTransport(_html_routes("example.com", body)))
        # 15 openings -> 11-50 band.
        assert await careers_jobs.count_job_listings(client, "https://example.com") == (11, 50)


@pytest.mark.asyncio
async def test_careers_jobs_none_when_no_listings() -> None:
    body = b"<html><body><p>We are not hiring right now.</p></body></html>"
    async with HomepageClient(
        user_agent=USER_AGENT, requests_per_second_per_domain=1000.0
    ) as client:
        _inject_transport(client, _MockTransport(_html_routes("example.com", body)))
        assert await careers_jobs.count_job_listings(client, "https://example.com") is None


@pytest.mark.asyncio
async def test_careers_jobs_none_without_website() -> None:
    async with HomepageClient(user_agent=USER_AGENT) as client:
        assert await careers_jobs.count_job_listings(client, None) is None


# ---------------------------------------------------------------------------
# github_org — resolve org via search, count public members via Link rel=last.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_org_maps_public_member_count() -> None:
    routes = [
        _Route(
            "search/users",
            b'{"items":[{"login":"vercel"}]}',
            content_type="application/json",
        ),
        _Route(
            "public_members",
            b"[{}]",
            content_type="application/json",
            headers={
                "Link": (
                    "<https://api.github.com/orgs/vercel/public_members"
                    '?per_page=1&page=69>; rel="last"'
                )
            },
        ),
    ]
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, _MockTransport(routes))
        # 69 public members -> 51-200 band.
        assert await github_org.get_employee_range(client, "Vercel", "tok") == (51, 200)


@pytest.mark.asyncio
async def test_github_org_returns_none_without_token() -> None:
    async with HomepageClient(user_agent=USER_AGENT) as client:
        # No transport needed — empty token short-circuits before any HTTP.
        assert await github_org.get_employee_range(client, "Vercel", "") is None


@pytest.mark.asyncio
async def test_github_org_returns_none_when_org_not_found() -> None:
    routes = [_Route("search/users", b'{"items":[]}', content_type="application/json")]
    async with HomepageClient(user_agent=USER_AGENT) as client:
        _inject_transport(client, _MockTransport(routes))
        assert await github_org.get_employee_range(client, "Nonexistent", "tok") is None
