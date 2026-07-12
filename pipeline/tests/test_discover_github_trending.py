"""Integration tests for the discover-github-trending stage.

``complete_json`` is monkeypatched (no real LLM calls); the trending page and
GitHub REST API are served by a mock transport. Requires DATABASE_URL (same
gating as the other stage tests) because the existing-company skip and
auto-create path exercise pg_trgm.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.llm.client import LLMError
from nous.llm.prompts.github_trending_company import TrendingCompanyJudgment
from nous.pipeline.discover_github_trending import (
    run_discover_github_trending,
)
from nous.sources.news import NewsClient
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

USER_AGENT = "nous-test test@example.com"

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"


# ---------------------------------------------------------------------------
# Transport + page builders
# ---------------------------------------------------------------------------


def _card(owner: str, name: str, description: str) -> str:
    return f"""
      <article class="Box-row">
        <h2 class="h3 lh-condensed"><a href="/{owner}/{name}">{owner} / {name}</a></h2>
        <p class="col-9 color-fg-muted my-1">{description}</p>
        <span itemprop="programmingLanguage">Rust</span>
        <a href="/{owner}/{name}/stargazers">1,200</a>
      </article>
    """


def _page(*cards: str) -> str:
    return f"<html><body><main>{''.join(cards)}</main></body></html>"


def _org(login: str, *, name: str | None = None, blog: str | None = None) -> str:
    return json.dumps(
        {"login": login, "type": "Organization", "name": name, "blog": blog}
    )


def _user(login: str) -> str:
    return json.dumps({"login": login, "type": "User"})


class _Transport(httpx.AsyncBaseTransport):
    """Serves robots + trending + per-login profile payloads by substring."""

    def __init__(self, page_html: str, profiles: dict[str, str]) -> None:
        self._page_html = page_html
        self._profiles = profiles

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "robots.txt" in url:
            return httpx.Response(200, content=ROBOTS_ALLOW_ALL.encode())
        if "github.com/trending" in url:
            return httpx.Response(
                200,
                content=self._page_html.encode(),
                headers={"content-type": "text/html"},
            )
        match = re.search(r"api\.github\.com/users/([^/?]+)", url)
        if match and match.group(1) in self._profiles:
            return httpx.Response(
                200,
                content=self._profiles[match.group(1)].encode(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404, content=b"Not Found")


def _inject(client: NewsClient, transport: httpx.AsyncBaseTransport) -> None:
    assert client._client is not None
    assert client._robots is not None
    client._client = httpx.AsyncClient(
        transport=transport, headers={"User-Agent": USER_AGENT}
    )
    client._robots._client = httpx.AsyncClient(
        transport=transport, headers={"User-Agent": USER_AGENT}
    )


# ---------------------------------------------------------------------------
# Fake LLM — canned judgment per owner login, with a call log
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Returns a canned judgment keyed by the login in the prompt."""

    def __init__(self, judgments: dict[str, TrendingCompanyJudgment | Exception]):
        self._judgments = judgments
        self.calls: list[str] = []

    async def __call__(
        self, prompt: str, schema: type[BaseModel], **kwargs: Any
    ) -> BaseModel:
        assert schema is TrendingCompanyJudgment
        match = re.search(r"GitHub owner login: (\S+)", prompt)
        assert match, "prompt must carry the owner login"
        login = match.group(1)
        self.calls.append(login)
        outcome = self._judgments[login]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _accept(name: str) -> TrendingCompanyJudgment:
    return TrendingCompanyJudgment(
        is_company=True, company_name=name, reason="Commercial devtool."
    )


_REJECT = TrendingCompanyJudgment(
    is_company=False, company_name=None, reason="Community project."
)
_UNCERTAIN = TrendingCompanyJudgment(is_company=None, company_name=None, reason=None)


async def _run(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    page: str,
    profiles: dict[str, str],
    llm: _FakeLLM,
    limit: int = 25,
) -> Any:
    monkeypatch.setattr(
        "nous.pipeline.discover_github_trending.complete_json", llm
    )
    client = NewsClient(user_agent=USER_AGENT)
    async with client:
        _inject(client, _Transport(page, profiles))
        return await run_discover_github_trending(
            db, client, github_token="tok", limit=limit
        )


async def _company_by_norm(db: AsyncSession, name: str) -> Company | None:
    result = await db.execute(
        select(Company).where(Company.normalized_name == normalize_name(name))
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Accept / reject / uncertain
# ---------------------------------------------------------------------------


async def test_accepted_org_creates_company(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _FakeLLM({"acme": _accept("Acme")})
    summary = await _run(
        db,
        monkeypatch,
        page=_page(_card("acme", "widget", "Open-core widget engine")),
        profiles={"acme": _org("acme", name="Acme Inc", blog="https://acme.dev")},
        llm=llm,
    )

    assert summary.repos_seen == 1
    assert summary.owners_judged == 1
    assert summary.owners_accepted == 1
    assert summary.companies_created == 1

    company = await _company_by_norm(db, "Acme")
    assert company is not None
    assert company.name == "Acme"
    assert company.website == "https://acme.dev"
    assert company.discovered_via == "github_trending"


async def test_rejected_and_uncertain_create_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _FakeLLM({"foolabs": _REJECT, "barlabs": _UNCERTAIN})
    summary = await _run(
        db,
        monkeypatch,
        page=_page(
            _card("foolabs", "toolkit", "A community toolkit"),
            _card("barlabs", "thing", "Unclear thing"),
        ),
        profiles={"foolabs": _org("foolabs"), "barlabs": _org("barlabs")},
        llm=llm,
    )

    assert summary.owners_judged == 2
    assert summary.owners_rejected == 1
    assert summary.owners_uncertain == 1
    assert summary.companies_created == 0
    assert await _company_by_norm(db, "foolabs") is None
    assert await _company_by_norm(db, "barlabs") is None


# ---------------------------------------------------------------------------
# Pre-LLM skips (no spend)
# ---------------------------------------------------------------------------


async def test_personal_account_skipped_without_llm(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _FakeLLM({})
    summary = await _run(
        db,
        monkeypatch,
        page=_page(_card("johndoe", "dotfiles", "My personal setup")),
        profiles={"johndoe": _user("johndoe")},
        llm=llm,
    )

    assert summary.owners_skipped_personal == 1
    assert summary.owners_judged == 0
    assert llm.calls == []


async def test_existing_company_skipped_before_llm(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add(
        Company(
            name="Acme",
            slug="acme",
            normalized_name=normalize_name("Acme"),
            discovered_via="vc_portfolio",
        )
    )
    await db.commit()

    llm = _FakeLLM({})
    summary = await _run(
        db,
        monkeypatch,
        page=_page(_card("acme", "widget", "Open-core widget engine")),
        profiles={"acme": _org("acme")},
        llm=llm,
    )

    assert summary.owners_skipped_existing == 1
    assert summary.owners_judged == 0
    assert llm.calls == []


async def test_profile_domain_match_skips_llm(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An org whose login/profile name is unknown but whose website domain
    already belongs to a catalog row is skipped without LLM spend."""
    db.add(
        Company(
            name="Totally Different Name",
            slug="totally-different-name",
            normalized_name=normalize_name("Totally Different Name"),
            website="https://acme.dev",
            discovered_via="vc_portfolio",
        )
    )
    await db.commit()

    llm = _FakeLLM({})
    summary = await _run(
        db,
        monkeypatch,
        page=_page(_card("acmehq", "widget", "Open-core widget engine")),
        profiles={"acmehq": _org("acmehq", blog="https://acme.dev")},
        llm=llm,
    )

    assert summary.owners_skipped_existing == 1
    assert llm.calls == []


# ---------------------------------------------------------------------------
# Idempotency: re-runs never duplicate
# ---------------------------------------------------------------------------


async def test_rerun_skips_created_company_and_spends_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _page(_card("acme", "widget", "Open-core widget engine"))
    profiles = {"acme": _org("acme", name="Acme", blog="https://acme.dev")}

    first_llm = _FakeLLM({"acme": _accept("Acme")})
    first = await _run(db, monkeypatch, page=page, profiles=profiles, llm=first_llm)
    assert first.companies_created == 1

    second_llm = _FakeLLM({})
    second = await _run(db, monkeypatch, page=page, profiles=profiles, llm=second_llm)
    assert second.owners_skipped_existing == 1
    assert second.owners_judged == 0
    assert second.companies_created == 0
    assert second_llm.calls == [], "re-run must not re-judge a known owner"

    rows = (
        (
            await db.execute(
                select(Company).where(
                    Company.normalized_name == normalize_name("Acme")
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "re-run must never duplicate"


async def test_accepted_owner_matching_existing_row_does_not_duplicate(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when every pre-LLM skip misses (different login, no profile), the
    auto-create path itself dedupes an accepted candidate by fuzzy name."""
    db.add(
        Company(
            name="LangChain",
            slug="langchain",
            normalized_name=normalize_name("LangChain"),
            discovered_via="techcrunch",
        )
    )
    await db.commit()

    # No profile for the owner (API 404s) → no domain/name pre-skip possible;
    # the LLM restyles the login to the canonical name the catalog already has.
    llm = _FakeLLM({"langchain-ai": _accept("LangChain")})
    summary = await _run(
        db,
        monkeypatch,
        page=_page(_card("langchain-ai", "langchain", "LLM app framework")),
        profiles={},
        llm=llm,
    )

    assert summary.owners_accepted == 1
    assert summary.companies_matched == 1
    assert summary.companies_created == 0

    rows = (
        (
            await db.execute(
                select(Company).where(
                    Company.normalized_name == normalize_name("LangChain")
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # First-discovery wins: the existing facet is not rewritten.
    assert rows[0].discovered_via == "techcrunch"


# ---------------------------------------------------------------------------
# Spend bound + failure isolation
# ---------------------------------------------------------------------------


async def test_limit_bounds_llm_judgments(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _FakeLLM({"one": _REJECT, "two": _REJECT})
    summary = await _run(
        db,
        monkeypatch,
        page=_page(
            _card("one", "alpha", "First tool"),
            _card("two", "beta", "Second tool"),
        ),
        profiles={"one": _org("one"), "two": _org("two")},
        llm=llm,
        limit=1,
    )

    assert summary.owners_seen == 2
    assert summary.owners_judged == 1
    assert len(llm.calls) == 1


async def test_llm_failure_is_isolated_per_owner(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _FakeLLM(
        {"broken": LLMError("boom"), "acme": _accept("Acme")}
    )
    summary = await _run(
        db,
        monkeypatch,
        page=_page(
            _card("broken", "alpha", "First tool"),
            _card("acme", "widget", "Open-core widget engine"),
        ),
        profiles={
            "broken": _org("broken"),
            "acme": _org("acme", blog="https://acme.dev"),
        },
        llm=llm,
    )

    assert summary.llm_failures == 1
    assert summary.companies_created == 1
    assert await _company_by_norm(db, "Acme") is not None
