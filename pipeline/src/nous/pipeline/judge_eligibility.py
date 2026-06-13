"""judge-eligibility pipeline stage (one-time backfill, safe to keep running).

Runs the is-this-a-startup judgment over companies that were enriched BEFORE
enrich-companies started making it (description present, eligibility never
checked). Reads stored raw_pages text; never re-writes descriptions.

Commit cadence: one commit per company. Rate-limit handling: stop the loop on
LLMRateLimitError (same pattern as enrich-companies). Selection is stamped via
eligibility_checked_at, so bounded daily runs drain the backlog and steady
state selects nothing (new enrichments stamp themselves).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from nous.db.models import Company, RawPage
from nous.llm.client import (
    MAX_PROMPT_INPUT_CHARS,
    LLMError,
    LLMParseError,
    LLMRateLimitError,
    complete_json,
)
from nous.llm.prompts.company_eligibility import EligibilityJudgment, build_prompt
from nous.util.text import extract_visible_text, truncate_to_chars

logger = logging.getLogger(__name__)


class JudgeEligibilitySummary(BaseModel):
    companies_judged: int = 0
    companies_excluded: int = 0
    llm_failures: int = 0
    skipped_rate_limited: int = 0


async def run_judge_eligibility(
    session: AsyncSession,
    *,
    limit: int | None = None,
) -> JudgeEligibilitySummary:
    summary = JudgeEligibilitySummary()

    stmt = (
        select(Company)
        .where(Company.description_short.is_not(None))
        .where(Company.eligibility_checked_at.is_(None))
        .where(Company.exclusion_reason.is_(None))
        .order_by(Company.name.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    companies = (await session.execute(stmt)).scalars().all()

    for company in companies:
        pages = (
            await session.execute(
                select(RawPage)
                .where(RawPage.company_id == company.id)
                .order_by(RawPage.url.asc())
            )
        ).scalars().all()
        parts = [extract_visible_text(p.content) for p in pages]
        cleaned = truncate_to_chars(
            "\n\n".join(p for p in parts if p), MAX_PROMPT_INPUT_CHARS
        )

        prompt = build_prompt(
            company_name=company.name,
            description=company.description_short or "",
            cleaned_text=cleaned or "(no scraped text on record)",
        )

        try:
            judgment: EligibilityJudgment = await complete_json(
                prompt, EligibilityJudgment
            )
        except LLMRateLimitError as exc:
            logger.warning(
                "LLM rate limit hit while judging %s — stopping loop. Raw: %s",
                company.name,
                exc,
            )
            summary.skipped_rate_limited += 1
            break
        except (LLMParseError, LLMError) as exc:
            logger.warning("LLM error judging %s: %s", company.name, exc)
            summary.llm_failures += 1
            continue

        now = datetime.now(tz=UTC)
        company.eligibility_checked_at = now
        if judgment.founded_year and not company.year_incorporated:
            company.year_incorporated = judgment.founded_year
        llm_country = (judgment.hq_country or "").strip().upper() or None
        if llm_country:
            company.hq_country = llm_country
        if judgment.is_startup is False:
            company.exclusion_reason = "not_a_startup"
            company.exclusion_detail = judgment.not_startup_reason
            company.excluded_at = now
            summary.companies_excluded += 1
        elif llm_country is not None and llm_country != "US":
            company.exclusion_reason = "non_us"
            company.exclusion_detail = f"website states HQ country {llm_country}"
            company.excluded_at = now
            summary.companies_excluded += 1

        session.add(company)
        try:
            await session.commit()
        except (StaleDataError, IntegrityError):
            await session.rollback()
            logger.warning(
                "Company %s disappeared mid-judge (likely a concurrent merge)"
                " — skipping.",
                company.id,
            )
            summary.llm_failures += 1
            continue
        summary.companies_judged += 1

    return summary
