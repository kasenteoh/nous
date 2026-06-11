"""Employee-count *proxy* from a company's GitHub organization.

For dev-tooling companies the GitHub org's public member count is a (lower-bound)
size signal. We resolve the org via the search API, then count public members
via the ``Link: rel="last"`` header on a ``per_page=1`` request, and map that to
a coarse band. Public membership is opt-in, so the real headcount is at least
the member count — the band is intentionally a floor, and this is the
last-resort signal.

Calls go straight to the GitHub REST API via the underlying httpx client (the
HomepageClient.fetch helper is built for GET-HTML and can't carry the
``Authorization`` header). api.github.com publishes no robots.txt. An empty
token, a miss, or any error returns ``None``.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from pydantic import BaseModel

from nous.sources.homepage import HomepageClient

logger = logging.getLogger(__name__)

_LINK_LAST_RE = re.compile(r'[?&]page=(\d+)>;\s*rel="last"')


class _GitHubUser(BaseModel):
    login: str


class _GitHubSearch(BaseModel):
    items: list[_GitHubUser] = []


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_employee_range(
    client: HomepageClient, company_name: str, github_token: str
) -> tuple[int, int] | None:
    """Return a coarse ``(min, max)`` band from the org's public member count."""
    if not github_token:
        return None
    try:
        login = await _resolve_org_login(client, company_name, github_token)
        if login is None:
            return None
        count = await _public_member_count(client, login, github_token)
    except Exception:  # noqa: BLE001 — best-effort source, degrade to None
        logger.debug("github_org: lookup failed for %s", company_name, exc_info=True)
        return None
    if count is None or count <= 0:
        return None
    return _bucket_from_member_count(count)


async def _resolve_org_login(
    client: HomepageClient, company_name: str, token: str
) -> str | None:
    underlying, _ = client._assert_open()
    query = quote(f"{company_name} type:org")
    resp = await underlying.get(
        f"https://api.github.com/search/users?q={query}&per_page=1",
        headers=_headers(token),
    )
    if resp.status_code != 200:
        return None
    search = _GitHubSearch.model_validate(resp.json())
    return search.items[0].login if search.items else None


async def _public_member_count(
    client: HomepageClient, login: str, token: str
) -> int | None:
    underlying, _ = client._assert_open()
    resp = await underlying.get(
        f"https://api.github.com/orgs/{login}/public_members?per_page=1",
        headers=_headers(token),
    )
    if resp.status_code != 200:
        return None
    # With per_page=1, the last page number equals the total member count.
    match = _LINK_LAST_RE.search(resp.headers.get("Link", ""))
    if match is not None:
        return int(match.group(1))
    # No Link header → 0 or 1 members fit on the single page.
    body = resp.json()
    return len(body) if isinstance(body, list) else None


def _bucket_from_member_count(count: int) -> tuple[int, int] | None:
    if count <= 0:
        return None
    if count <= 10:
        return (1, 10)
    if count <= 50:
        return (11, 50)
    if count <= 200:
        return (51, 200)
    if count <= 500:
        return (201, 500)
    if count <= 1000:
        return (501, 1000)
    return (1001, 5000)
