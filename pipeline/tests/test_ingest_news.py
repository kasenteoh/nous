"""Tests for the ingest-news stage.

Unit tests (always run) cover the LLM-backed TC company extractor with
``complete_json`` mocked. DB-integration tests (skipped without DATABASE_URL)
cover persistence, idempotency, and the TC broad-ingest auto-create path.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle
from nous.llm.prompts.news_company import HeadlineCompany
from nous.pipeline.ingest_news import (
    _extract_company_from_tc_result,
    run_ingest_news,
)
from nous.sources.news import NewsArticleResult


def _tc_result(title: str, snippet: str = "snippet") -> NewsArticleResult:
    return NewsArticleResult(
        url="https://techcrunch.com/x",
        title=title,
        source="techcrunch.com",
        published_date=None,
        raw_content=snippet,
    )


def _mock_headline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_funding: bool,
    name: str | None,
) -> None:
    """Patch complete_json in the ingest-news module to return a canned HeadlineCompany."""

    async def _fake(prompt: str, schema: type) -> HeadlineCompany:
        assert schema is HeadlineCompany
        return HeadlineCompany(is_funding_announcement=is_funding, company_name=name)

    monkeypatch.setattr("nous.pipeline.ingest_news.complete_json", _fake)


# ---------------------------------------------------------------------------
# Unit tests for the LLM-backed TC company extractor
# ---------------------------------------------------------------------------


class TestExtractCompanyFromTcResult:
    async def test_returns_clean_name_for_funding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_headline(monkeypatch, is_funding=True, name="Stord")
        name = await _extract_company_from_tc_result(
            _tc_result("Amazon fulfillment competitor Stord raises $250M")
        )
        assert name == "Stord"

    async def test_non_funding_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_headline(monkeypatch, is_funding=False, name=None)
        name = await _extract_company_from_tc_result(
            _tc_result("The state of AI in 2026")
        )
        assert name is None

    async def test_funding_but_no_name_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_headline(monkeypatch, is_funding=True, name=None)
        name = await _extract_company_from_tc_result(
            _tc_result("An unnamed startup raised a big round")
        )
        assert name is None

    async def test_blank_name_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_headline(monkeypatch, is_funding=True, name="   ")
        name = await _extract_company_from_tc_result(
            _tc_result("Something raises money")
        )
        assert name is None


# ---------------------------------------------------------------------------
# DB-gated integration tests
# ---------------------------------------------------------------------------


pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(name: str, slug: str | None = None) -> Company:
    return Company(
        name=name,
        slug=slug or f"{name.lower().replace(' ', '-')}-{os.urandom(3).hex()}",
        normalized_name=name.lower(),
        hq_country="US",
    )


class _MockNewsClient:
    """Stand-in for NewsClient. Records every fetch_article_body call."""

    def __init__(
        self,
        *,
        rss_results: dict[str, list[NewsArticleResult]] | None = None,
        bodies: dict[str, str | None] | None = None,
    ) -> None:
        self._rss_results = rss_results or {}
        self._bodies = bodies or {}
        self.body_fetches: list[str] = []

    async def __aenter__(self) -> _MockNewsClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def google_news_rss(
        self, query: str, lookback_days: int = 7
    ) -> list[NewsArticleResult]:
        return self._rss_results.get(query, [])

    async def fetch_article_body(self, url: str) -> str | None:
        self.body_fetches.append(url)
        return self._bodies.get(url)


@pytestmark_db
async def test_ingest_inserts_articles_for_existing_company(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = _make_company("Acme Inc")
    db.add(company)
    await db.flush()
    await db.commit()

    article = NewsArticleResult(
        url="https://news.example.com/acme-raises-50m",
        title="Acme Inc raises $50M",
        source="news.example.com",
        published_date=date(2026, 5, 1),
        raw_content="snippet",
    )
    client = _MockNewsClient(
        rss_results={'"Acme Inc" funding': [article]},
        bodies={article.url: "This is the article body" * 50},  # >500 chars
    )

    async def _no_tc(*args: Any, **kwargs: Any) -> list[NewsArticleResult]:
        return []

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _no_tc
    )

    summary = await run_ingest_news(db, client, include_techcrunch_broad=True)

    assert summary.companies_queried >= 1
    assert summary.articles_inserted == 1

    rows = (await db.execute(select(NewsArticle))).scalars().all()
    assert any(r.url == article.url for r in rows)


@pytestmark_db
async def test_ingest_is_idempotent_on_rerun(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = _make_company("Idemp Co")
    db.add(company)
    await db.flush()
    await db.commit()

    article = NewsArticleResult(
        url="https://news.example.com/idemp-raise",
        title="Idemp Co raises $10M",
        source="news.example.com",
        published_date=date(2026, 5, 1),
        raw_content="snippet",
    )
    client = _MockNewsClient(
        rss_results={'"Idemp Co" funding': [article]},
        bodies={article.url: "Body text " * 100},
    )

    async def _no_tc(*args: Any, **kwargs: Any) -> list[NewsArticleResult]:
        return []

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _no_tc
    )

    s1 = await run_ingest_news(db, client, include_techcrunch_broad=False)
    assert s1.articles_inserted == 1
    s2 = await run_ingest_news(db, client, include_techcrunch_broad=False)
    assert s2.articles_inserted == 0  # already stored


@pytestmark_db
async def test_tc_broad_auto_creates_company(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TC article whose company isn't in the DB should auto-create it."""
    tc_article = NewsArticleResult(
        url="https://techcrunch.com/2026/05/01/newco-raises-100m",
        title="NewCo raises $100M Series B",
        source="techcrunch.com",
        published_date=date(2026, 5, 1),
        raw_content="snippet",
    )

    async def _tc(*args: Any, **kwargs: Any) -> list[NewsArticleResult]:
        return [tc_article]

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _tc
    )
    _mock_headline(monkeypatch, is_funding=True, name="NewCo")

    client = _MockNewsClient(
        rss_results={},
        bodies={tc_article.url: "TC body text " * 100},
    )

    summary = await run_ingest_news(db, client, include_techcrunch_broad=True)
    assert summary.auto_created_companies == 1
    assert summary.articles_inserted == 1

    rows = (
        await db.execute(select(Company).where(Company.name == "NewCo"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].discovered_via == "techcrunch"


@pytestmark_db
async def test_tc_non_funding_is_skipped(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TC item the LLM judges as not-a-funding-announcement is counted, not inserted."""
    bad_article = NewsArticleResult(
        url="https://techcrunch.com/2026/05/01/the-future-of-ai",
        title="The future of AI in 2026",
        source="techcrunch.com",
        published_date=date(2026, 5, 1),
        raw_content="snippet",
    )

    async def _tc(*args: Any, **kwargs: Any) -> list[NewsArticleResult]:
        return [bad_article]

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _tc
    )
    _mock_headline(monkeypatch, is_funding=False, name=None)

    client = _MockNewsClient(rss_results={}, bodies={})
    summary = await run_ingest_news(db, client, include_techcrunch_broad=True)
    assert summary.tc_skipped_no_company == 1
    assert summary.articles_inserted == 0
    assert summary.auto_created_companies == 0


@pytestmark_db
async def test_skips_articles_with_thin_body(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = _make_company("Thin Body Co")
    db.add(company)
    await db.flush()
    await db.commit()

    article = NewsArticleResult(
        url="https://news.example.com/thin",
        title="Thin Body Co raises $1M",
        source="news.example.com",
        published_date=None,
        raw_content="snippet",
    )
    client = _MockNewsClient(
        rss_results={'"Thin Body Co" funding': [article]},
        bodies={article.url: None},  # fetch_article_body returned None
    )

    async def _no_tc(*args: Any, **kwargs: Any) -> list[NewsArticleResult]:
        return []

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _no_tc
    )

    summary = await run_ingest_news(db, client, include_techcrunch_broad=False)
    assert summary.articles_skipped_thin == 1
    assert summary.articles_inserted == 0
