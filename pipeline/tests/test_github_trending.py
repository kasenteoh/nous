"""Tests for nous.sources.github_trending.

Same mock-transport pattern as test_venturebeat.py — no real network calls.
The fixture is a trimmed live capture of https://github.com/trending
(2026-07-11, 24 repo cards): page shell/scripts removed, ``<main>`` kept
verbatim, tracking attributes and SVG path data stripped (verified at capture
time to parse identically to the raw page).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from nous.sources.github_trending import (
    GITHUB_TRENDING_URL,
    GitHubOwnerProfile,
    TrendingRepo,
    fetch_owner_profile,
    fetch_trending_repos,
    parse_trending_repos,
)
from nous.sources.news import NewsClient

FIXTURES = Path(__file__).parent / "fixtures"
TRENDING_HTML = (FIXTURES / "github_trending.html").read_text()

USER_AGENT = "nous-test test@example.com"

ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"

# The wildcard block of github.com/robots.txt at capture time (2026-07-11):
# /trending itself is allowed; any URL carrying a since= query is not.
ROBOTS_GITHUB_LIKE = "User-agent: *\nDisallow: /*since=*\n"


class _Route:
    def __init__(
        self,
        substring: str,
        *,
        status: int = 200,
        body: str = "",
        content_type: str = "text/html",
        raise_network_error: bool = False,
    ) -> None:
        self.substring = substring
        self.status = status
        self.body = body
        self.content_type = content_type
        self.raise_network_error = raise_network_error
        self.call_count = 0
        self.last_headers: dict[str, str] = {}


class _MockTransport(httpx.AsyncBaseTransport):
    """Dispatches to first matching route; 404 by default."""

    def __init__(self, routes: list[_Route]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for r in self._routes:
            if r.substring in url_str:
                r.call_count += 1
                r.last_headers = dict(request.headers)
                if r.raise_network_error:
                    raise httpx.ConnectError("Connection refused")
                resp = httpx.Response(
                    r.status,
                    content=r.body.encode(),
                    headers={"content-type": r.content_type},
                )
                if r.status >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {r.status}", request=request, response=resp
                    )
                return resp
        return httpx.Response(404, content=b"Not Found")


def _inject(client: NewsClient, transport: httpx.AsyncBaseTransport) -> None:
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


def _routes(*, page_status: int = 200, page_body: str = TRENDING_HTML) -> list[_Route]:
    return [
        _Route("github.com/robots.txt", status=200, body=ROBOTS_GITHUB_LIKE),
        _Route("github.com/trending", status=page_status, body=page_body),
    ]


# ---------------------------------------------------------------------------
# URL constant — the robots finding, pinned
# ---------------------------------------------------------------------------


def test_trending_url_is_the_daily_page_without_since_param() -> None:
    """github.com/robots.txt allows /trending but disallows /*since=* — the
    ?since=weekly variant must never come back."""
    assert GITHUB_TRENDING_URL == "https://github.com/trending"
    assert "since=" not in GITHUB_TRENDING_URL
    assert "?" not in GITHUB_TRENDING_URL


# ---------------------------------------------------------------------------
# Canary: fixture parse floor + well-formed fields
# ---------------------------------------------------------------------------


def test_parse_fixture_yields_all_cards() -> None:
    repos = parse_trending_repos(TRENDING_HTML)
    assert len(repos) == 24, f"expected 24 cards in the capture, got {len(repos)}"
    for r in repos:
        assert isinstance(r, TrendingRepo)
        assert r.owner.strip()
        assert r.name.strip()
        assert "/" not in r.owner
        assert "/" not in r.name


def test_parse_fixture_known_repo_fields() -> None:
    """Spot-check one well-known card end to end."""
    repos = {(r.owner, r.name): r for r in parse_trending_repos(TRENDING_HTML)}
    prisma = repos[("prisma", "prisma")]
    assert prisma.language == "TypeScript"
    assert prisma.stars == 47231
    assert prisma.description is not None
    assert prisma.description.startswith("Next-generation ORM")


def test_parse_handles_missing_description_language_and_stars() -> None:
    """Sparse synthetic card: absent optional fields become None, and a card
    whose href is not owner/repo is skipped rather than crashing."""
    html = """
    <html><body>
      <article class="Box-row">
        <h2 class="h3 lh-condensed"><a href="/acme/widget">acme / widget</a></h2>
      </article>
      <article class="Box-row">
        <h2 class="h3 lh-condensed"><a href="/not-a-repo">weird</a></h2>
      </article>
    </body></html>
    """
    repos = parse_trending_repos(html)
    assert len(repos) == 1
    assert repos[0] == TrendingRepo(
        owner="acme", name="widget", description=None, language=None, stars=None
    )


def test_parse_empty_page_returns_empty_list() -> None:
    assert parse_trending_repos("<html><body></body></html>") == []


# ---------------------------------------------------------------------------
# fetch_trending_repos — transport discipline
# ---------------------------------------------------------------------------


async def test_fetch_trending_parses_live_capture() -> None:
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes()))
        repos = await fetch_trending_repos(client)
    assert len(repos) == 24


async def test_fetch_trending_robots_block_returns_empty() -> None:
    routes = [
        _Route("github.com/robots.txt", status=200, body=ROBOTS_DISALLOW_ALL),
        _Route("github.com/trending", status=200, body=TRENDING_HTML),
    ]
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(routes))
        repos = await fetch_trending_repos(client)
    assert repos == []
    assert routes[1].call_count == 0, "page must not be fetched under a robots block"


async def test_fetch_trending_http_error_returns_empty() -> None:
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(_routes(page_status=404)))
        repos = await fetch_trending_repos(client)
    assert repos == []


async def test_fetch_trending_network_error_returns_empty() -> None:
    routes = [
        _Route("github.com/robots.txt", status=200, body=ROBOTS_GITHUB_LIKE),
        _Route("github.com/trending", raise_network_error=True),
    ]
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(routes))
        repos = await fetch_trending_repos(client)
    assert repos == []


# ---------------------------------------------------------------------------
# fetch_owner_profile — REST API discipline
# ---------------------------------------------------------------------------


_ORG_PAYLOAD = json.dumps(
    {
        "login": "acme",
        "type": "Organization",
        "name": "Acme Inc",
        "blog": "https://acme.dev",
        "bio": "Widgets as a service",
        "followers": 12,  # extra fields must be ignored
    }
)


async def test_fetch_owner_profile_parses_org() -> None:
    routes = [
        _Route(
            "api.github.com/users/acme",
            body=_ORG_PAYLOAD,
            content_type="application/json",
        )
    ]
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(routes))
        profile = await fetch_owner_profile(client, "acme")
    assert profile == GitHubOwnerProfile(
        login="acme",
        type="Organization",
        name="Acme Inc",
        blog="https://acme.dev",
        bio="Widgets as a service",
    )
    # No token → no Authorization header.
    assert "authorization" not in routes[0].last_headers


async def test_fetch_owner_profile_sends_token_when_given() -> None:
    routes = [
        _Route(
            "api.github.com/users/acme",
            body=_ORG_PAYLOAD,
            content_type="application/json",
        )
    ]
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _MockTransport(routes))
        profile = await fetch_owner_profile(client, "acme", github_token="tok123")
    assert profile is not None
    assert routes[0].last_headers.get("authorization") == "Bearer tok123"


async def test_fetch_owner_profile_miss_and_error_return_none() -> None:
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(
            client,
            _MockTransport([_Route("api.github.com/users/gone", status=404)]),
        )
        assert await fetch_owner_profile(client, "gone") is None

    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(
            client,
            _MockTransport(
                [_Route("api.github.com/users/flaky", raise_network_error=True)]
            ),
        )
        assert await fetch_owner_profile(client, "flaky") is None
