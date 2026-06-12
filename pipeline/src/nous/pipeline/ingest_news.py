"""ingest-news pipeline stage.

For each company we already track, query Google News RSS for funding-related
articles in the lookback window, fetch the article bodies, and persist them
to ``news_articles``. Then, if requested, sweep the TechCrunch venture-tag
broad feed to catch funding announcements for companies we don't yet have —
asking the LLM to identify the funded company from each headline + snippet and
auto-creating a row.

Commit cadence: per-article so a mid-run crash leaves a clean state.

Idempotency: ``news_articles.url`` is unique on canonical form, so re-ingest
is a no-op via the upsert path (we use a pre-check rather than ON CONFLICT
because we don't want to fetch the article body if we already have it).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle
from nous.db.upsert import auto_create_company
from nous.llm.client import LLMError, LLMRateLimitError, complete_json
from nous.llm.prompts.news_company import HeadlineCompany, build_prompt
from nous.sources.news import NewsArticleResult, NewsClient
from nous.sources.techcrunch import fetch_techcrunch_funding_articles

logger = logging.getLogger(__name__)


async def _extract_company_from_tc_result(result: NewsArticleResult) -> str | None:
    """Use the LLM to identify the funded company from a TC feed item.

    Returns the clean company name, or None when the item isn't a funding
    announcement or the model can't identify a company. LLM exceptions
    (rate limit, parse failure) propagate to the caller, which decides
    whether to stop the sweep or skip the one item.
    """
    extraction = await complete_json(
        build_prompt(title=result.title, snippet=result.raw_content),
        HeadlineCompany,
    )
    if not extraction.is_funding_announcement:
        return None
    name = (extraction.company_name or "").strip()
    return name or None


class IngestNewsSummary(BaseModel):
    companies_queried: int = 0
    articles_seen: int = 0
    articles_kept: int = 0
    articles_inserted: int = 0
    articles_skipped_thin: int = 0
    auto_created_companies: int = 0
    tc_skipped_no_company: int = 0


async def _article_already_stored(session: AsyncSession, url: str) -> bool:
    """Cheap existence check on the unique URL — avoids the body fetch."""
    stmt = select(exists().where(NewsArticle.url == url))
    return bool(await session.scalar(stmt))


async def _ingest_one_article(
    session: AsyncSession,
    client: NewsClient,
    company_id: UUID,
    result: NewsArticleResult,
    summary: IngestNewsSummary,
) -> None:
    """Fetch the article body and persist to news_articles. Per-row commit."""
    if await _article_already_stored(session, result.url):
        return
    body = await client.fetch_article_body(result.url)
    if body is None:
        summary.articles_skipped_thin += 1
        return

    article = NewsArticle(
        company_id=company_id,
        url=result.url,
        title=result.title,
        source=result.source,
        published_date=result.published_date,
        raw_content=body,
    )
    session.add(article)
    await session.commit()
    summary.articles_inserted += 1


async def run_ingest_news(
    session: AsyncSession,
    client: NewsClient,
    *,
    lookback_days: int = 7,
    include_techcrunch_broad: bool = True,
    max_companies: int | None = None,
    similarity_threshold: float = 0.85,
) -> IngestNewsSummary:
    """Fetch per-company Google News results + optional TechCrunch broad sweep.

    Per-company path: query Google News RSS for ``"<name>" funding``, filter
    to funding-keyword matches (done inside ``NewsClient.google_news_rss``),
    and persist the body for each new URL. Companies are taken least-recently
    -checked first (news_checked_at NULLS FIRST) and stamped on every attempt,
    so a ``max_companies``-bounded daily run rotates through the whole table
    every ~table/limit days — at the default workflow limit that matches the
    7-day lookback window, so no announcement is missed while the per-day
    request count to Google News stays small and polite.

    TC broad-tag path: fetch the TC venture feed; for each article whose
    title parses to a candidate company name, find-or-auto-create the
    company, then persist the article.
    """
    summary = IngestNewsSummary()

    company_stmt = select(Company).order_by(
        Company.news_checked_at.asc().nulls_first()
    )
    if max_companies is not None:
        company_stmt = company_stmt.limit(max_companies)
    company_result = await session.execute(company_stmt)
    companies = company_result.scalars().all()

    for company in companies:
        summary.companies_queried += 1
        query = f'"{company.name}" funding'
        try:
            results = await client.google_news_rss(query, lookback_days=lookback_days)
        except Exception:
            logger.exception("google_news_rss failed for %s", company.name)
            results = []
        for result in results:
            summary.articles_seen += 1
            summary.articles_kept += 1  # already keyword-filtered upstream
            await _ingest_one_article(session, client, company.id, result, summary)
        # Stamp the attempt (success or failure) so the rotation advances —
        # a head-of-line company whose query keeps failing must not pin the
        # whole rotation in place. Commit per company, matching the
        # per-article commits above.
        company.news_checked_at = datetime.now(tz=UTC)
        session.add(company)
        await session.commit()

    if include_techcrunch_broad:
        try:
            tc_results = await fetch_techcrunch_funding_articles(
                client, lookback_days=lookback_days
            )
        except Exception:
            logger.exception("techcrunch venture feed fetch failed")
            tc_results = []

        for result in tc_results:
            summary.articles_seen += 1
            if await _article_already_stored(session, result.url):
                continue
            try:
                candidate = await _extract_company_from_tc_result(result)
            except LLMRateLimitError:
                # Stop making LLM calls for the rest of the sweep; the next run
                # picks up where this left off (unstored URLs are reprocessed).
                logger.warning(
                    "LLM rate limit hit during TC company extraction — "
                    "stopping the TechCrunch sweep."
                )
                break
            except LLMError:
                logger.exception(
                    "LLM company extraction failed for TC item %r", result.title
                )
                summary.tc_skipped_no_company += 1
                continue

            if candidate is None:
                summary.tc_skipped_no_company += 1
                continue
            # Fetch the body BEFORE auto-creating a company. A failed fetch
            # (robots block, 4xx, thin content) should not leave an orphan
            # company row with discovered_via='techcrunch' and zero supporting
            # articles. If the body succeeds the URL is dedupable next run;
            # if not, the worst case is one wasted feed read per week.
            try:
                body = await client.fetch_article_body(result.url)
            except Exception:
                logger.exception(
                    "TC body fetch raised for %r (candidate=%r)",
                    result.title,
                    candidate,
                )
                continue
            if body is None:
                summary.articles_skipped_thin += 1
                continue
            try:
                company, created = await auto_create_company(
                    session,
                    name=candidate,
                    website=None,
                    discovered_via="techcrunch",
                    similarity_threshold=similarity_threshold,
                )
                if created:
                    summary.auto_created_companies += 1
                summary.articles_kept += 1
                article = NewsArticle(
                    company_id=company.id,
                    url=result.url,
                    title=result.title,
                    source=result.source,
                    published_date=result.published_date,
                    raw_content=body,
                )
                session.add(article)
                await session.commit()
                summary.articles_inserted += 1
            except Exception:
                logger.exception(
                    "TC broad-ingest failed for %r (candidate=%r)",
                    result.title,
                    candidate,
                )
                await session.rollback()

    return summary
