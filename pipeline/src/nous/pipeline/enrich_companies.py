"""enrich-companies pipeline stage.

For each company that has raw_pages but no recent LLM enrichment, call the LLM
to generate descriptions and metadata.

Commit cadence: one commit per company so a mid-run crash leaves a clean state.

Rate-limit handling: on LLMRateLimitError, stop the entire loop immediately
rather than keep hammering the free-tier quota.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.llm.client import LLMError, LLMParseError, LLMRateLimitError, complete_json
from nous.llm.prompts.company_description import CompanyDescription, build_prompt
from nous.util.text import extract_visible_text, truncate_to_chars

logger = logging.getLogger(__name__)

# Minimum cleaned text length; below this we consider the page too thin to enrich.
_MIN_TEXT_CHARS = 200


def _normalize_tag(tag: str) -> str:
    """Lowercase + replace whitespace runs with hyphens."""
    tag = tag.lower().strip()
    return re.sub(r"\s+", "-", tag)


class EnrichSummary(BaseModel):
    companies_seen: int = 0
    companies_enriched: int = 0
    llm_failures: int = 0
    skipped_no_text: int = 0
    skipped_rate_limited: int = 0


async def run_enrich_companies(
    session: AsyncSession,
    *,
    max_companies: int | None = None,
    refetch_after_days: int = 90,
) -> EnrichSummary:
    """Enrich companies that have raw_pages but no recent description.

    A company is eligible when:
    - At least one RawPage row exists for it, AND
    - description_short IS NULL OR last_enriched_at < (now - refetch_after_days).
    """
    summary = EnrichSummary()

    cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)

    stmt = (
        select(Company)
        .where(
            exists().where(RawPage.company_id == Company.id)
        )
        .where(
            (Company.description_short.is_(None))
            | (Company.last_enriched_at.is_(None))
            | (Company.last_enriched_at < cutoff)
        )
    )
    if max_companies is not None:
        stmt = stmt.limit(max_companies)

    result = await session.execute(stmt)
    companies = result.scalars().all()

    for company in companies:
        summary.companies_seen += 1

        # Load all raw_pages for this company, sorted by url (/ sorts before /about etc.)
        pages_result = await session.execute(
            select(RawPage)
            .where(RawPage.company_id == company.id)
            .order_by(RawPage.url.asc())
        )
        pages = pages_result.scalars().all()

        if not pages:
            # Should not happen given the outer filter, but guard defensively.
            summary.skipped_no_text += 1
            continue

        # Concatenate visible text from all pages.
        parts = [extract_visible_text(page.content) for page in pages]
        combined = "\n\n".join(p for p in parts if p)
        cleaned = truncate_to_chars(combined, 32_000)

        if len(cleaned) < _MIN_TEXT_CHARS:
            logger.info(
                "Company %s has too little text (%d chars) — skipping enrichment",
                company.name,
                len(cleaned),
            )
            summary.skipped_no_text += 1
            continue

        prompt = build_prompt(company_name=company.name, cleaned_text=cleaned)

        try:
            description: CompanyDescription = await complete_json(prompt, CompanyDescription)
        except LLMRateLimitError as exc:
            # Surface the full 429 body so we can see *which* quota tripped
            # (per-minute RPM vs per-day RPD vs token-per-minute TPM) instead
            # of guessing from the bare "rate limit" signal.
            logger.warning(
                "LLM rate limit hit while enriching %s — stopping loop to"
                " avoid further quota exhaustion. Raw error: %s",
                company.name,
                exc,
            )
            summary.skipped_rate_limited += 1
            # Stop the entire loop — don't keep hammering the free tier.
            break
        except (LLMParseError, LLMError) as exc:
            logger.warning("LLM error enriching %s: %s", company.name, exc)
            summary.llm_failures += 1
            continue

        # Normalize tags: lowercase + hyphenated.
        normalized_tags = [_normalize_tag(t) for t in description.tags if t.strip()]

        now = datetime.now(tz=UTC)
        company.description_short = description.description_short
        company.description_long = description.description_long
        company.primary_category = description.primary_category
        company.tags = normalized_tags
        company.last_enriched_at = now
        company.last_enriched_payload = description.model_dump(mode="json")

        session.add(company)
        await session.commit()
        summary.companies_enriched += 1

    return summary
