"""Tests for the ingest-time entity guard (same-name different-entity killer).

Decision matrix on check_article_entity (strong-corroboration bypasses the
LLM; suspect/weak adjudicate; no-profile attaches) plus integration through
run_ingest_news pinning the storage semantics: a wrong-entity article never
persists, an LLM error skips WITHOUT storing so the next sweep retries.
Requires DATABASE_URL for the integration tests; the decision tests are
DB-free but share the file.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle
from nous.llm.client import LLMError
from nous.llm.prompts.article_subject_match import ArticleSubjectMatch
from nous.pipeline.entity_guard import check_article_entity
from nous.pipeline.ingest_news import run_ingest_news
from nous.sources.news import NewsArticleResult
from nous.util.slugify import normalize_name

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_EDTECH_WONDER_DESC = (
    "Wonder is an online education platform connecting students with expert "
    "tutors for personalized learning journeys."
)
_FOOD_WONDER_BODY = (
    "Wonder raises $650M at a $9B valuation. The food hall and delivery "
    "startup founded by Marc Lore operates dozens of locations serving "
    "meals from celebrity chefs. Wonder will open more kitchens."
) * 3


def _co(name: str, description: str | None, **kw: Any) -> Company:
    return Company(
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{os.urandom(3).hex()}",
        normalized_name=normalize_name(name),
        hq_country="US",
        description_short=description,
        **kw,
    )


def _llm_must_not_be_called(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(prompt: str, schema: type) -> ArticleSubjectMatch:
        raise AssertionError("LLM adjudication must not run for this case")

    monkeypatch.setattr("nous.pipeline.entity_guard.complete_json", _boom)


def _llm_returns(
    monkeypatch: pytest.MonkeyPatch, verdict: ArticleSubjectMatch
) -> list[str]:
    prompts: list[str] = []

    async def _fake(prompt: str, schema: type) -> ArticleSubjectMatch:
        prompts.append(prompt)
        return verdict

    monkeypatch.setattr("nous.pipeline.entity_guard.complete_json", _fake)
    return prompts


async def test_strong_corroboration_attaches_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _llm_must_not_be_called(monkeypatch)
    company = _co("Wave", "Wave is building a mobile money network across "
                  "Africa with free deposits and flat-fee transfers.")
    decision = await check_article_entity(
        company,
        title="Wave raises $137M to expand mobile money across Africa",
        text=(
            "Wave, the Senegal fintech, offers free deposits and flat-fee "
            "money transfers across its agent network in eight countries."
        ),
    )
    assert decision.attach is True
    assert decision.adjudicated is False
    assert decision.reason == "strong-corroboration"


async def test_no_profile_attaches_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _llm_must_not_be_called(monkeypatch)
    company = _co("Husk Co", None)
    decision = await check_article_entity(
        company, title="Husk Co raises $5M", text="Husk Co raised $5M."
    )
    assert decision.attach is True
    assert decision.reason == "no-profile"


async def test_weak_corroboration_adjudicates_and_drops_on_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The food-Wonder shape: bare proper mentions, zero context overlap —
    the cheap layer's pinned blind spot goes to the LLM, which sees a food
    company on an edtech profile."""
    prompts = _llm_returns(
        monkeypatch,
        ArticleSubjectMatch(
            is_subject=False,
            confidence="high",
            other_entity_name="Wonder (food delivery)",
        ),
    )
    company = _co("Wonder", _EDTECH_WONDER_DESC)
    decision = await check_article_entity(
        company, title="Wonder raises $650M", text=_FOOD_WONDER_BODY
    )
    assert decision.attach is False
    assert decision.adjudicated is True
    assert decision.other_entity == "Wonder (food delivery)"
    # The prompt carries both identities for the model to compare.
    assert "education platform" in prompts[0]
    assert "food hall" in prompts[0]


async def test_cheap_suspect_adjudicates(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _llm_returns(
        monkeypatch,
        ArticleSubjectMatch(is_subject=False, confidence="high",
                            other_entity_name="Primary Wave Music"),
    )
    company = _co("Wave", "Wave is building a mobile money network across "
                  "Africa with free deposits and flat-fee transfers.")
    decision = await check_article_entity(
        company,
        title="Primary Wave Announces $2.2B Raise",
        text=(
            "Primary Wave announced a $2.2 billion raise led by Brookfield. "
            "The deal makes Primary Wave one of the largest independent "
            "music publishers. Primary Wave's catalog spans decades."
        ),
    )
    assert decision.attach is False
    assert len(prompts) == 1


async def test_llm_match_attaches_and_low_confidence_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = _co("Wonder", _EDTECH_WONDER_DESC)
    _llm_returns(
        monkeypatch,
        ArticleSubjectMatch(is_subject=True, confidence="medium"),
    )
    ok = await check_article_entity(
        company, title="Wonder raises $30M", text=_FOOD_WONDER_BODY
    )
    assert ok.attach is True
    assert ok.reason == "llm-match-medium"

    _llm_returns(
        monkeypatch,
        ArticleSubjectMatch(is_subject=True, confidence="low"),
    )
    weak = await check_article_entity(
        company, title="Wonder raises $30M", text=_FOOD_WONDER_BODY
    )
    assert weak.attach is False  # a low-confidence yes is not an attach


async def test_llm_error_reports_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(prompt: str, schema: type) -> ArticleSubjectMatch:
        raise LLMError("boom")

    monkeypatch.setattr("nous.pipeline.entity_guard.complete_json", _fail)
    company = _co("Wonder", _EDTECH_WONDER_DESC)
    decision = await check_article_entity(
        company, title="Wonder raises $650M", text=_FOOD_WONDER_BODY
    )
    assert decision.attach is False
    assert decision.llm_error is True


async def test_rate_limit_opens_circuit_and_allow_llm_false_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 marks the decision rate_limited; with allow_llm=False the guard
    never calls the LLM for adjudication-requiring articles but cheap
    verdicts still attach."""
    from nous.llm.client import LLMRateLimitError

    calls: list[str] = []

    async def _rl(prompt: str, schema: type) -> ArticleSubjectMatch:
        calls.append(prompt)
        raise LLMRateLimitError("429")

    monkeypatch.setattr("nous.pipeline.entity_guard.complete_json", _rl)
    company = _co("Wonder", _EDTECH_WONDER_DESC)
    d1 = await check_article_entity(
        company, title="Wonder raises $650M", text=_FOOD_WONDER_BODY
    )
    assert d1.rate_limited is True and d1.llm_error is True
    assert len(calls) == 1

    # Circuit open: no further LLM call, adjudication-requiring skips…
    d2 = await check_article_entity(
        company,
        title="Wonder raises $650M",
        text=_FOOD_WONDER_BODY,
        allow_llm=False,
    )
    assert d2.attach is False and d2.llm_error is True
    assert len(calls) == 1  # untouched

    # …but a no-profile company still attaches without the LLM.
    husk = _co("Husk Co", None)
    d3 = await check_article_entity(
        husk, title="Husk Co raises $5M", text="Husk Co raised $5M.",
        allow_llm=False,
    )
    assert d3.attach is True


# ---------------------------------------------------------------------------
# Integration through run_ingest_news — storage semantics
# ---------------------------------------------------------------------------


class _GuardMockClient:
    def __init__(self, rss: dict[str, list[NewsArticleResult]], body: str) -> None:
        self._rss = rss
        self._body = body

    async def google_news_rss(
        self, query: str, lookback_days: int = 7
    ) -> list[NewsArticleResult]:
        return self._rss.get(query, [])

    async def fetch_article_body(self, url: str) -> str | None:
        return self._body

    async def resolve_article(self, url: str) -> None:
        return None


def _wonder_result() -> NewsArticleResult:
    return NewsArticleResult(
        url="https://food.example.com/wonder-650m",
        title="Wonder raises $650M at $9B valuation",
        source="food.example.com",
        published_date=date(2026, 7, 16),
        raw_content="snippet",
    )


@pytestmark_db
async def test_wrong_entity_article_never_persists(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _co("Wonder", _EDTECH_WONDER_DESC)
    db.add(company)
    await db.commit()

    _llm_returns(
        monkeypatch,
        ArticleSubjectMatch(is_subject=False, confidence="high",
                            other_entity_name="Wonder (food)"),
    )

    async def _no_feed(*a: Any, **k: Any) -> list[NewsArticleResult]:
        return []

    for feed in (
        "fetch_techcrunch_funding_articles",
        "fetch_siliconangle_funding_articles",
        "fetch_prnewswire_funding_articles",
        "fetch_crunchbase_news_funding_articles",
        "fetch_venturebeat_funding_articles",
        "fetch_geekwire_funding_articles",
    ):
        monkeypatch.setattr(f"nous.pipeline.ingest_news.{feed}", _no_feed)

    client = _GuardMockClient(
        {f'"{company.name}" funding': [_wonder_result()]}, _FOOD_WONDER_BODY
    )
    summary = await run_ingest_news(db, client)  # type: ignore[arg-type]

    assert summary.articles_adjudicated == 1
    assert summary.articles_skipped_wrong_entity == 1
    assert summary.articles_inserted == 0
    rows = (await db.execute(select(NewsArticle))).scalars().all()
    assert rows == []


@pytestmark_db
async def test_broad_feed_guard_drops_wrong_entity_for_existing_company(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broad-feed path: a title-parsed name matching an EXISTING company
    goes through the guard; a rejected article must not persist and must not
    disturb the rest of the sweep."""
    company = _co("Wonder", _EDTECH_WONDER_DESC)
    db.add(company)
    await db.commit()

    feed_item = NewsArticleResult(
        url="https://tc.example.com/wonder-650m",
        title="Wonder raises $650M for food halls",
        source="techcrunch.com",
        published_date=date(2026, 7, 16),
        raw_content="snippet",
    )

    async def _one_feed(*a: Any, **k: Any) -> list[NewsArticleResult]:
        return [feed_item]

    async def _no_feed(*a: Any, **k: Any) -> list[NewsArticleResult]:
        return []

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _one_feed
    )
    for feed in (
        "fetch_siliconangle_funding_articles",
        "fetch_prnewswire_funding_articles",
        "fetch_crunchbase_news_funding_articles",
        "fetch_venturebeat_funding_articles",
        "fetch_geekwire_funding_articles",
    ):
        monkeypatch.setattr(f"nous.pipeline.ingest_news.{feed}", _no_feed)

    async def _extract(result: NewsArticleResult) -> str | None:
        return "Wonder"

    monkeypatch.setattr(
        "nous.pipeline.ingest_news._extract_company_from_tc_result", _extract
    )
    _llm_returns(
        monkeypatch,
        ArticleSubjectMatch(is_subject=False, confidence="high",
                            other_entity_name="Wonder (food)"),
    )

    client = _GuardMockClient({}, _FOOD_WONDER_BODY)
    summary = await run_ingest_news(
        db, client, max_companies=0  # broad path only
    )  # type: ignore[arg-type]

    assert summary.articles_adjudicated == 1
    assert summary.articles_skipped_wrong_entity == 1
    assert summary.articles_inserted == 0
    assert summary.auto_created_companies == 0  # matched, not created
    assert (await db.execute(select(NewsArticle))).scalars().all() == []


@pytestmark_db
async def test_llm_error_skips_unstored_then_next_sweep_attaches(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient LLM failure: nothing persists (no wrong attach, no permanent
    drop) — and once the LLM is healthy the SAME article ingests."""
    company = _co("Wonder", _EDTECH_WONDER_DESC)
    db.add(company)
    await db.commit()

    async def _no_feed(*a: Any, **k: Any) -> list[NewsArticleResult]:
        return []

    for feed in (
        "fetch_techcrunch_funding_articles",
        "fetch_siliconangle_funding_articles",
        "fetch_prnewswire_funding_articles",
        "fetch_crunchbase_news_funding_articles",
        "fetch_venturebeat_funding_articles",
        "fetch_geekwire_funding_articles",
    ):
        monkeypatch.setattr(f"nous.pipeline.ingest_news.{feed}", _no_feed)

    async def _fail(prompt: str, schema: type) -> ArticleSubjectMatch:
        raise LLMError("transient")

    monkeypatch.setattr("nous.pipeline.entity_guard.complete_json", _fail)
    client = _GuardMockClient(
        {f'"{company.name}" funding': [_wonder_result()]}, _FOOD_WONDER_BODY
    )
    summary = await run_ingest_news(db, client)  # type: ignore[arg-type]
    assert summary.articles_skipped_guard_error == 1
    assert summary.articles_inserted == 0
    assert (await db.execute(select(NewsArticle))).scalars().all() == []

    # LLM heals; the URL was never stored, so the next sweep retries and
    # (this time adjudicated as a genuine match) attaches.
    _llm_returns(
        monkeypatch, ArticleSubjectMatch(is_subject=True, confidence="high")
    )
    summary2 = await run_ingest_news(db, client)  # type: ignore[arg-type]
    assert summary2.articles_inserted == 1
    rows = (await db.execute(select(NewsArticle))).scalars().all()
    assert len(rows) == 1
