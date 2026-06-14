"""Tests for the ingest-news stage.

Unit tests (always run) cover the LLM-backed TC company extractor with
``complete_json`` mocked. DB-integration tests (skipped without DATABASE_URL)
cover persistence, idempotency, and the TC broad-ingest auto-create path.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
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
from nous.sources.news import NewsArticleResult, ResolvedArticle
from nous.util.slugify import normalize_name


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
        normalized_name=normalize_name(name),
        hq_country="US",
    )


class _MockNewsClient:
    """Stand-in for NewsClient. Records every fetch_article_body / resolve_article call."""

    def __init__(
        self,
        *,
        rss_results: dict[str, list[NewsArticleResult]] | None = None,
        bodies: dict[str, str | None] | None = None,
        resolved: dict[str, ResolvedArticle | None] | None = None,
    ) -> None:
        self._rss_results = rss_results or {}
        self._bodies = bodies or {}
        self._resolved = resolved or {}
        self.body_fetches: list[str] = []
        self.resolve_calls: list[str] = []

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

    async def resolve_article(self, url: str) -> ResolvedArticle | None:
        self.resolve_calls.append(url)
        return self._resolved.get(url)


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


@pytestmark_db
async def test_resolves_google_news_redirect_and_fetches_body(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Google News redirect URL is RESOLVED to its real publisher article: the
    redirect is followed, the destination page is fetched, and the stored
    raw_content is the full article body (>=MIN_BODY_CHARS), not the thin RSS
    snippet. The stored URL + source reflect the resolved publisher, so the
    funding-extraction LLM sees rich text and emits higher-confidence rounds
    (Task A1)."""
    company = _make_company("Redirect Co")
    db.add(company)
    await db.flush()
    await db.commit()

    redirect_url = "https://news.google.com/rss/articles/CBMiOPAQUE?oc=5"
    real_body = (
        "Redirect Co, the warehousing startup, announced today that it has "
        "raised a $30M Series B round led by Acme Ventures, with participation "
        "from Beta Capital and Gamma Partners. " * 12
    )  # >> MIN_BODY_CHARS
    article = NewsArticleResult(
        url=redirect_url,
        title="Redirect Co Raises $30M Series B - Reuters",
        source="news.google.com",  # RSS source before resolution
        published_date=date(2026, 6, 1),
        raw_content="Redirect Co announced a $30M Series B led by Acme Ventures.",
    )
    client = _MockNewsClient(
        rss_results={'"Redirect Co" funding': [article]},
        resolved={
            redirect_url: ResolvedArticle(
                url="https://realpub.com/redirect-co-series-b",
                source="realpub.com",
                body=real_body,
            )
        },
    )

    async def _no_tc(*args: Any, **kwargs: Any) -> list[NewsArticleResult]:
        return []

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _no_tc
    )

    summary = await run_ingest_news(db, client, include_techcrunch_broad=False)

    assert summary.articles_inserted == 1
    assert summary.articles_skipped_thin == 0
    assert client.resolve_calls == [redirect_url]  # the redirect was resolved
    assert client.body_fetches == []  # resolution handles the fetch itself

    stored = (
        (
            await db.execute(
                select(NewsArticle).where(NewsArticle.company_id == company.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(stored) == 1
    # The REAL article body is stored, not the thin RSS snippet.
    assert len(stored[0].raw_content) >= 500
    assert "participation from Beta Capital" in stored[0].raw_content
    # The resolved publisher is preserved for attribution + dedup.
    assert stored[0].source == "realpub.com"
    assert stored[0].url == "https://realpub.com/redirect-co-series-b"


@pytestmark_db
async def test_google_news_redirect_falls_back_to_headline_when_unresolvable(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the Google News redirect can't be resolved (robots-block, non-HTML,
    thin page, or fetch error → resolve_article returns None), ingest falls back
    to storing the headline (+ snippet) so the per-company funding signal is
    never lost. The headline still carries the company + round + amount, which
    is enough for extract-funding."""
    company = _make_company("Fallback Co")
    db.add(company)
    await db.flush()
    await db.commit()

    redirect_url = "https://news.google.com/rss/articles/CBMiFALLBACK?oc=5"
    article = NewsArticleResult(
        url=redirect_url,
        title="Fallback Co Raises $12M Seed - Reuters",
        source="reuters.com",
        published_date=date(2026, 6, 1),
        raw_content="Fallback Co announced a $12M Seed led by Acme Ventures.",
    )
    client = _MockNewsClient(
        rss_results={'"Fallback Co" funding': [article]},
        resolved={redirect_url: None},  # resolution failed
    )

    async def _no_tc(*args: Any, **kwargs: Any) -> list[NewsArticleResult]:
        return []

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _no_tc
    )

    summary = await run_ingest_news(db, client, include_techcrunch_broad=False)

    assert summary.articles_inserted == 1
    assert summary.articles_skipped_thin == 0
    assert client.resolve_calls == [redirect_url]
    assert client.body_fetches == []  # no second wasted fetch on fallback

    stored = (
        (
            await db.execute(
                select(NewsArticle).where(NewsArticle.company_id == company.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(stored) == 1
    assert "Seed" in stored[0].raw_content  # the headline became the content
    assert stored[0].source == "reuters.com"  # RSS publisher preserved
    assert stored[0].url == redirect_url  # original redirect URL kept


@pytestmark_db
async def test_tc_broad_failed_body_does_not_auto_create(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the TC body fetch returns None, do NOT auto-create the company.

    Pre-fix behavior was to auto-create the row first, then attempt the body
    fetch. A robots-block/4xx/thin-content body left an orphan company with
    discovered_via='techcrunch' and zero supporting articles, and the same
    URL was refetched every weekly run forever.
    """
    tc_article = NewsArticleResult(
        url="https://techcrunch.com/2026/05/01/ghostco-raises-100m",
        title="GhostCo raises $100M Series B",
        source="techcrunch.com",
        published_date=date(2026, 5, 1),
        raw_content="snippet",
    )

    async def _tc(*args: Any, **kwargs: Any) -> list[NewsArticleResult]:
        return [tc_article]

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _tc
    )
    _mock_headline(monkeypatch, is_funding=True, name="GhostCo")

    # Body fetch returns None (robots-block, thin content, 4xx, etc.)
    client = _MockNewsClient(rss_results={}, bodies={tc_article.url: None})

    summary = await run_ingest_news(db, client, include_techcrunch_broad=True)
    assert summary.articles_skipped_thin == 1
    assert summary.auto_created_companies == 0
    assert summary.articles_inserted == 0

    rows = (
        await db.execute(select(Company).where(Company.name == "GhostCo"))
    ).scalars().all()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Per-company rotation (news_checked_at)
# ---------------------------------------------------------------------------


class _QueryLoggingNewsClient(_MockNewsClient):
    """Mock that records every google_news_rss query string."""

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    async def google_news_rss(
        self, query: str, lookback_days: int = 7
    ) -> list[NewsArticleResult]:
        self.queries.append(query)
        return []


@pytestmark_db
async def test_rotation_prefers_never_checked_companies(
    db: AsyncSession,
) -> None:
    """With max_companies set, never-checked companies are queried before
    recently checked ones, so a daily limited run rotates through the whole
    table instead of re-querying the same head every day."""
    checked = _make_company("Checked Recently Inc")
    checked.news_checked_at = datetime.now(tz=UTC)
    never = _make_company("Never Checked Inc")
    db.add_all([checked, never])
    await db.flush()
    await db.commit()

    client = _QueryLoggingNewsClient()
    await run_ingest_news(
        db,
        client,  # type: ignore[arg-type]
        include_techcrunch_broad=False,
        max_companies=1,
    )

    assert client.queries == ['"Never Checked Inc" funding']


@pytestmark_db
async def test_queried_company_is_stamped_even_with_no_results(
    db: AsyncSession,
) -> None:
    """news_checked_at is stamped on every attempt so the rotation advances."""
    company = _make_company("Stamped Inc")
    db.add(company)
    await db.flush()
    await db.commit()

    client = _QueryLoggingNewsClient()
    await run_ingest_news(
        db,
        client,  # type: ignore[arg-type]
        include_techcrunch_broad=False,
        max_companies=10,
    )

    await db.refresh(company)
    assert company.news_checked_at is not None


# ---------------------------------------------------------------------------
# TC threshold plumbing
# ---------------------------------------------------------------------------


async def test_tc_path_passes_similarity_threshold_to_auto_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_ingest_news must forward similarity_threshold= to auto_create_company
    on the TC broad-ingest path — the same way refresh_vc_portfolios.py does.

    This is a unit test: we mock session.execute (returns no companies so the
    per-company loop is skipped) and auto_create_company (captures kwargs).
    No real DB needed.
    """
    from unittest.mock import AsyncMock, MagicMock

    tc_article = NewsArticleResult(
        url="https://techcrunch.com/2026/06/01/thresholdco-raises-50m",
        title="ThresholdCo raises $50M Series A",
        source="techcrunch.com",
        published_date=date(2026, 6, 1),
        raw_content="snippet",
    )

    async def _tc(*args: Any, **kwargs: Any) -> list[NewsArticleResult]:
        return [tc_article]

    monkeypatch.setattr(
        "nous.pipeline.ingest_news.fetch_techcrunch_funding_articles", _tc
    )
    _mock_headline(monkeypatch, is_funding=True, name="ThresholdCo")

    # Capture auto_create_company calls.
    received_kwargs: list[dict[str, Any]] = []

    async def _capture_auto_create(
        session: Any, *, name: str, website: Any, discovered_via: str,
        similarity_threshold: float = 0.85,
    ) -> tuple[Any, bool]:
        received_kwargs.append({
            "name": name,
            "similarity_threshold": similarity_threshold,
        })
        fake_company = MagicMock()
        fake_company.id = "00000000-0000-0000-0000-000000000001"
        return fake_company, True

    monkeypatch.setattr("nous.pipeline.ingest_news.auto_create_company", _capture_auto_create)

    # Also patch _article_already_stored to return False (new URL), and
    # session.add / session.commit to no-ops so no DB is needed.
    monkeypatch.setattr(
        "nous.pipeline.ingest_news._article_already_stored",
        AsyncMock(return_value=False),
    )

    # Mock the session: execute returns an empty result (no existing companies
    # in the per-company path), scalar / add / commit are no-ops.
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=mock_result)
    fake_session.add = MagicMock()
    fake_session.commit = AsyncMock()
    fake_session.rollback = AsyncMock()

    client = _MockNewsClient(
        rss_results={},
        bodies={tc_article.url: "TC body text " * 100},
    )

    custom_threshold = 0.72
    await run_ingest_news(
        fake_session,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        include_techcrunch_broad=True,
        similarity_threshold=custom_threshold,
    )

    assert len(received_kwargs) == 1, "auto_create_company should have been called once"
    assert received_kwargs[0]["similarity_threshold"] == custom_threshold, (
        f"Expected threshold {custom_threshold!r}, got "
        f"{received_kwargs[0]['similarity_threshold']!r}"
    )
