"""extract-funding pipeline stage.

For each unprocessed NewsArticle, call Gemini with the funding-extraction
prompt and persist the structured round/investor data. Marks the article
``processed=true`` either way (success, "not a funding announcement", or
low-confidence skip) so re-runs only revisit truly-unprocessed rows.

Idempotency:
- ``processed`` flag is the work-queue gate; once set, the article is never
  re-extracted.
- ``reconcile_funding_round`` merges into existing rounds within the
  proximity window rather than inserting duplicates.
- ``upsert_investor`` is keyed on the canonicalized name.
- ``link_round_investor`` uses ON CONFLICT to merge `is_lead` (sticky-true).

Quota discipline (spec §11):
- Hard cap on articles processed per run (default 1000 = M3 weekly Gemini
  budget; see plan).
- On LLMRateLimitError, stop the loop immediately — same pattern as M2's
  enrich-companies.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle
from nous.db.upsert import (
    link_round_investor,
    reconcile_funding_round,
    upsert_investor,
)
from nous.llm.client import LLMError, LLMParseError, LLMRateLimitError, complete_json
from nous.llm.prompts.funding_extraction import FundingExtraction, build_prompt

logger = logging.getLogger(__name__)


class ExtractFundingSummary(BaseModel):
    articles_processed: int = 0
    funding_rounds_created: int = 0
    funding_rounds_merged: int = 0
    investors_created: int = 0
    investor_links_created: int = 0
    llm_failures: int = 0
    skipped_not_funding: int = 0
    skipped_low_confidence: int = 0
    skipped_rate_limited: int = 0


async def run_extract_funding(
    session: AsyncSession,
    *,
    limit: int = 1000,
    skip_low_confidence: bool = True,
    proximity_days: int = 60,
) -> ExtractFundingSummary:
    """Walk unprocessed news_articles oldest-first and extract funding rounds."""
    summary = ExtractFundingSummary()

    stmt = (
        select(NewsArticle)
        .where(NewsArticle.processed.is_(False))
        .order_by(NewsArticle.published_date.desc().nulls_last(), NewsArticle.created_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    articles = result.scalars().all()

    for article in articles:
        # Need the owning company's name for the prompt.
        company = await session.get(Company, article.company_id)
        if company is None:
            logger.warning(
                "news_article %s references missing company_id %s — marking processed",
                article.id,
                article.company_id,
            )
            article.processed = True
            session.add(article)
            await session.commit()
            continue

        prompt = build_prompt(
            company_name=company.name,
            article_text=article.raw_content,
        )

        try:
            extraction: FundingExtraction = await complete_json(prompt, FundingExtraction)
        except LLMRateLimitError:
            logger.warning(
                "Gemini rate limit hit while extracting funding for %s — stopping"
                " loop to avoid further quota exhaustion.",
                company.name,
            )
            summary.skipped_rate_limited += 1
            break
        except (LLMParseError, LLMError) as exc:
            logger.warning(
                "LLM error extracting funding from %s: %s", article.url, exc
            )
            summary.llm_failures += 1
            # Leave processed=false so a future run with a fixed prompt can retry.
            continue

        summary.articles_processed += 1

        if not extraction.is_funding_announcement:
            summary.skipped_not_funding += 1
            article.processed = True
            session.add(article)
            await session.commit()
            continue

        if skip_low_confidence and extraction.confidence == "low":
            summary.skipped_low_confidence += 1
            article.processed = True
            session.add(article)
            await session.commit()
            continue

        funding_round, created = await reconcile_funding_round(
            session,
            company_id=company.id,
            extraction=extraction,
            primary_news_url=article.url,
            proximity_days=proximity_days,
        )
        if created:
            summary.funding_rounds_created += 1
        else:
            summary.funding_rounds_merged += 1

        for investor_name in extraction.lead_investors:
            if not investor_name.strip():
                continue
            try:
                investor, inv_created = await upsert_investor(
                    session, name=investor_name
                )
            except ValueError:
                continue
            if inv_created:
                summary.investors_created += 1
            await link_round_investor(
                session,
                funding_round_id=funding_round.id,
                investor_id=investor.id,
                is_lead=True,
            )
            summary.investor_links_created += 1

        for investor_name in extraction.other_investors:
            if not investor_name.strip():
                continue
            try:
                investor, inv_created = await upsert_investor(
                    session, name=investor_name
                )
            except ValueError:
                continue
            if inv_created:
                summary.investors_created += 1
            await link_round_investor(
                session,
                funding_round_id=funding_round.id,
                investor_id=investor.id,
                is_lead=False,
            )
            summary.investor_links_created += 1

        article.processed = True
        session.add(article)
        await session.commit()

    return summary
