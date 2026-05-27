"""ingest-news pipeline stage.

For each company we already track, query Google News RSS for funding-related
articles in the lookback window, fetch the article bodies, and persist them
to ``news_articles``. Then, if requested, sweep the TechCrunch venture-tag
broad feed to catch funding announcements for companies we don't yet have —
extracting the company name from each article title and auto-creating a row.

Commit cadence: per-article so a mid-run crash leaves a clean state.

Idempotency: ``news_articles.url`` is unique on canonical form, so re-ingest
is a no-op via the upsert path (we use a pre-check rather than ON CONFLICT
because we don't want to fetch the article body if we already have it).
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle
from nous.db.upsert import auto_create_company
from nous.sources.news import NewsArticleResult, NewsClient
from nous.sources.techcrunch import fetch_techcrunch_funding_articles

logger = logging.getLogger(__name__)


# Verbs that typically appear in TC funding-announcement headlines. Used to
# extract a candidate company name from a title like "Stord raises $250M".
# Listed in rough order of frequency; not exhaustive.
_TC_FUNDING_VERBS = (
    "raises",
    "raised",
    "closes",
    "closed",
    "secures",
    "secured",
    "announces",
    "announced",
    "lands",
    "landed",
    "scores",
    "scored",
    "nabs",
    "nabbed",
    "snags",
    "snagged",
    "bags",
    "bagged",
    "completes",
    "completed",
    "picks up",
)

_TC_TITLE_RE = re.compile(
    r"^([A-Z][A-Za-z0-9.\-'&\s]+?)\s+(?:" + "|".join(_TC_FUNDING_VERBS) + r")\b",
    re.IGNORECASE,
)


def _extract_company_name_from_tc_title(title: str) -> str | None:
    """Best-effort extraction of a company name from a TC headline.

    Returns None when the title doesn't follow the "<Company> <verb>" shape
    or the candidate is implausibly short. The funding-extraction LLM call
    will independently verify whether the article is actually about that
    company (via the is_funding_announcement field), so a wrong extraction
    here costs a wasted LLM call but never produces a false funding round.
    """
    match = _TC_TITLE_RE.match(title)
    if not match:
        return None
    candidate = match.group(1).strip()
    if len(candidate) < 2 or len(candidate) > 80:
        return None
    return candidate


class IngestNewsSummary(BaseModel):
    companies_queried: int = 0
    articles_seen: int = 0
    articles_kept: int = 0
    articles_inserted: int = 0
    articles_skipped_thin: int = 0
    auto_created_companies: int = 0
    tc_skipped_unparseable_title: int = 0


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
) -> IngestNewsSummary:
    """Fetch per-company Google News results + optional TechCrunch broad sweep.

    Per-company path: for each Company in the DB (optionally limited),
    query Google News RSS for ``"<name>" funding``, filter to funding-keyword
    matches (done inside ``NewsClient.google_news_rss``), and persist the
    body for each new URL.

    TC broad-tag path: fetch the TC venture feed; for each article whose
    title parses to a candidate company name, find-or-auto-create the
    company, then persist the article.
    """
    summary = IngestNewsSummary()

    company_stmt = select(Company)
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
            continue
        for result in results:
            summary.articles_seen += 1
            summary.articles_kept += 1  # already keyword-filtered upstream
            await _ingest_one_article(session, client, company.id, result, summary)

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
            candidate = _extract_company_name_from_tc_title(result.title)
            if candidate is None:
                summary.tc_skipped_unparseable_title += 1
                continue
            try:
                company, created = await auto_create_company(
                    session,
                    name=candidate,
                    website=None,
                    discovered_via="techcrunch",
                )
                if created:
                    summary.auto_created_companies += 1
                summary.articles_kept += 1
                await _ingest_one_article(session, client, company.id, result, summary)
            except Exception:
                logger.exception(
                    "TC broad-ingest failed for %r (candidate=%r)",
                    result.title,
                    candidate,
                )
                await session.rollback()

    return summary
