"""GitHub trending-page adapter: trending repos → company-candidate signals.

Devtools companies (Supabase-class) often trend on GitHub months before
TechCrunch covers them, so the trending page is a discovery surface the
news/VC paths miss. This module supplies the raw signals; the LLM judgment
gate and auto-create live in ``nous.pipeline.discover_github_trending``.

Source choice (robots.txt, checked live 2026-07-11):

- ``https://github.com/robots.txt`` does NOT disallow ``/trending`` for
  ``User-agent: *`` — the plain daily trending page is crawlable.
- ``Disallow: /*since=*`` DOES block the ``/trending?since=weekly`` variant
  (and any other ``since=`` query), so this adapter uses the daily window
  only. The weekly discovery cadence still sees fresh repos: the daily list
  turns over far faster than once a week.
- The runtime fetch goes through :meth:`NewsClient.fetch_text`, which
  re-checks robots.txt on every request — if GitHub later disallows
  ``/trending``, the fetch degrades to ``[]`` instead of violating it.

Owner profiles come from the GitHub REST API (``/users/{login}``), which
carries the org-vs-user distinction the trending HTML lacks, plus the org's
display name and ``blog`` (website) field. api.github.com publishes no
robots.txt (same precedent as ``nous.sources.github_org``); requests still
pay the per-domain throttle and carry our identifying User-Agent, and the
optional ``GITHUB_TOKEN`` lifts the unauthenticated 60 req/h ceiling.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser

from nous.sources.news import NewsClient, RobotsBlockedError
from nous.util.ssrf import BlockedAddressError

logger = logging.getLogger(__name__)

# Daily trending page. Deliberately NO ``?since=weekly`` — robots.txt
# disallows ``/*since=*`` (see module docstring).
GITHUB_TRENDING_URL = "https://github.com/trending"

GITHUB_API_BASE = "https://api.github.com"

_WHITESPACE_RE = re.compile(r"\s+")


class TrendingRepo(BaseModel):
    """One repository card parsed from the trending page.

    Everything here comes verbatim from the page — no fetched-later fields.
    ``stars`` is the repo's total star count (the stargazers link text);
    the "N stars today" figure is not kept, since the LLM gate cares about
    what the project *is*, not its day-to-day velocity.
    """

    owner: str  # login of the owning account ("supabase")
    name: str  # repository name ("supabase")
    description: str | None
    language: str | None
    stars: int | None


class GitHubOwnerProfile(BaseModel):
    """Subset of the REST ``/users/{login}`` payload the mapper needs.

    ``type`` is ``"Organization"`` or ``"User"`` — the personal-account
    filter the trending HTML cannot provide. ``name``/``blog``/``bio`` are
    the org-authored profile fields fed to the LLM gate; ``blog`` is the
    org's self-declared website (candidate ``website``, still verified
    downstream by resolve-homepages / scrape-homepages).
    """

    login: str
    type: str
    name: str | None = None
    blog: str | None = None
    bio: str | None = None


def _collapse(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _parse_star_count(text: str) -> int | None:
    """Parse "17,789" → 17789; None on anything non-numeric."""
    cleaned = text.strip().replace(",", "")
    return int(cleaned) if cleaned.isdigit() else None


def parse_trending_repos(html: str) -> list[TrendingRepo]:
    """Parse the trending page into :class:`TrendingRepo` cards.

    Pure and fixture-tested. Selector map (capture 2026-07-11, 24 cards):

    - card:        ``article.Box-row``
    - repo link:   ``h2 a[href="/owner/repo"]``
    - description: the card's first ``<p>``
    - language:    ``[itemprop="programmingLanguage"]``
    - stars:       ``a[href="/owner/repo/stargazers"]`` text

    Cards whose href does not split into exactly ``owner/repo`` are skipped
    (defensive: a layout change should degrade to fewer cards, not crash —
    the adapter-health probe catches a collapse to zero).
    """
    tree = HTMLParser(html)
    repos: list[TrendingRepo] = []
    for article in tree.css("article.Box-row"):
        link = article.css_first("h2 a[href]")
        if link is None:
            continue
        href = (link.attributes.get("href") or "").strip()
        parts = [p for p in href.strip("/").split("/") if p]
        if len(parts) != 2:
            continue
        owner, name = parts

        description: str | None = None
        desc_node = article.css_first("p")
        if desc_node is not None:
            description = _collapse(desc_node.text()) or None

        language: str | None = None
        lang_node = article.css_first('[itemprop="programmingLanguage"]')
        if lang_node is not None:
            language = _collapse(lang_node.text()) or None

        stars: int | None = None
        star_node = article.css_first(f'a[href="/{owner}/{name}/stargazers"]')
        if star_node is not None:
            stars = _parse_star_count(star_node.text())

        repos.append(
            TrendingRepo(
                owner=owner,
                name=name,
                description=description,
                language=language,
                stars=stars,
            )
        )
    return repos


async def fetch_trending_repos(client: NewsClient) -> list[TrendingRepo]:
    """Fetch + parse the trending page.

    Returns an empty list on robots block, HTTP error, or network failure —
    the caller treats this like any other empty-source case rather than
    erroring out the whole discovery run (same contract as the feed
    adapters). The adapter-health probe fetches the URL separately and does
    surface these failures.
    """
    try:
        html = await client.fetch_text(GITHUB_TRENDING_URL)
    except RobotsBlockedError:
        logger.warning("GitHub trending page blocked by robots.txt")
        return []
    except (httpx.HTTPStatusError, httpx.RequestError, BlockedAddressError) as exc:
        logger.warning("GitHub trending fetch failed: %s", exc)
        return []
    return parse_trending_repos(html)


def _api_headers(github_token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    return headers


async def fetch_owner_profile(
    client: NewsClient, login: str, *, github_token: str = ""
) -> GitHubOwnerProfile | None:
    """Fetch ``/users/{login}`` from the GitHub REST API; None on any failure.

    Best-effort by design: an API miss must not block discovery — the caller
    falls back to judging the owner from the trending-page signals alone.
    Unlike :mod:`nous.sources.github_org`, the request goes through the
    client's throttle slot so api.github.com pays the same per-domain toll
    as every other host (the User-Agent header rides on the underlying
    httpx client).
    """
    underlying, _ = client._assert_open()
    url = f"{GITHUB_API_BASE}/users/{quote(login)}"
    try:
        async with client._http.slot(url):
            resp = await underlying.get(url, headers=_api_headers(github_token))
        if resp.status_code != 200:
            logger.info(
                "github owner profile %s returned HTTP %d", login, resp.status_code
            )
            return None
        return GitHubOwnerProfile.model_validate(resp.json())
    except Exception:  # noqa: BLE001 — best-effort source, degrade to None
        logger.debug("github owner profile lookup failed for %s", login, exc_info=True)
        return None
