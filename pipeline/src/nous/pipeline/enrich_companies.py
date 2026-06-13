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
from sqlalchemy import ColumnElement, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from nous.db.models import Company, RawPage
from nous.db.upsert import replace_people
from nous.llm.client import (
    MAX_PROMPT_INPUT_CHARS,
    LLMError,
    LLMParseError,
    LLMRateLimitError,
    complete_json,
)
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
    # NOT disjoint from companies_enriched: a not_a_startup/non_us row still has
    # its description written (then hidden by exclusion_reason), so it counts in
    # both buckets. companies_seen is the only true total.
    companies_excluded: int = 0
    people_written: int = 0
    llm_failures: int = 0
    skipped_no_text: int = 0
    skipped_bad_website: int = 0
    skipped_rate_limited: int = 0


async def run_enrich_companies(
    session: AsyncSession,
    *,
    max_companies: int | None = None,
    refetch_after_days: int | None = None,
) -> EnrichSummary:
    """Enrich companies that have raw_pages but no current description.

    Description + people (written together from the same scraped pages) are
    "stable" data — written once, not refreshed every run (the volatile data is
    funding + competitors). A company is eligible when:
    - At least one RawPage row exists for it, AND
    - description_short IS NULL, OR ``refetch_after_days`` is provided and
      last_enriched_at is older than that.

    With the default ``refetch_after_days=None`` the stage is write-once: it only
    enriches companies that have never been enriched. To backfill people onto
    rows enriched before this stage wrote them (or to refresh descriptions),
    run with ``--refetch-after-days 0`` to force re-enrichment of everyone.
    """
    summary = EnrichSummary()

    conditions: list[ColumnElement[bool]] = [Company.description_short.is_(None)]
    if refetch_after_days is not None:
        cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)
        conditions.append(Company.last_enriched_at.is_(None))
        conditions.append(Company.last_enriched_at < cutoff)

    # Require at least one page with enough stored text to plausibly clear
    # the _MIN_TEXT_CHARS bar. raw_pages.content holds extracted visible text
    # (see scrape-homepages), so length() is a faithful proxy. Without this,
    # thin-text companies — which nothing ever stamps — re-enter the selection
    # every run and eventually monopolize the LIMIT below. The in-loop check
    # on the concatenated text remains as the authoritative guard.
    stmt = (
        select(Company)
        .where(
            exists().where(
                RawPage.company_id == Company.id,
                func.length(RawPage.content) >= _MIN_TEXT_CHARS,
            )
        )
        .where(or_(*conditions))
        .where(Company.exclusion_reason.is_(None))
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
        cleaned = truncate_to_chars(combined, MAX_PROMPT_INPUT_CHARS)

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

        now = datetime.now(tz=UTC)

        if description.website_state != "ok":
            # The scraped site is parked/for-sale, unrelated, or contentless —
            # the URL is wrong or worthless, which says nothing about the
            # company itself. Reject the URL, clear the website, and drop the
            # junk pages so the selection stops re-picking this company until
            # a new site is resolved + scraped. Junk prose is never published.
            logger.info(
                "Company %s website_state=%s — clearing website %s",
                company.name,
                description.website_state,
                company.website,
            )
            if company.website:
                company.rejected_urls = [
                    *(company.rejected_urls or []),
                    company.website,
                ]
            company.website = None
            company.website_resolved_at = None
            await session.execute(
                delete(RawPage).where(RawPage.company_id == company.id)
            )
            session.add(company)
            try:
                await session.commit()
            except (StaleDataError, IntegrityError):
                await session.rollback()
                logger.warning(
                    "Company %s disappeared mid-enrich (likely a concurrent"
                    " merge) — skipping.",
                    company.id,
                )
            summary.skipped_bad_website += 1
            continue

        # Normalize tags: lowercase + hyphenated.
        normalized_tags = [_normalize_tag(t) for t in description.tags if t.strip()]

        company.description_short = description.description_short
        company.description_long = description.description_long
        company.primary_category = description.primary_category
        company.tags = normalized_tags
        company.last_enriched_at = now
        company.last_enriched_payload = description.model_dump(mode="json")

        # Location + industry from the website. Only fill these when the LLM
        # returned a value AND the column is currently empty — don't clobber
        # values already set by another source.
        if description.hq_city and not company.hq_city:
            company.hq_city = description.hq_city
        if description.hq_state and not company.hq_state:
            company.hq_state = description.hq_state
        if description.industry and not company.industry_group:
            company.industry_group = description.industry
        if (company.hq_city or company.hq_state) and not company.hq_country:
            company.hq_country = "US"

        # Eligibility judgment (spec 2026-06-12). Runs only on website_state
        # == "ok" — a parked/unrelated page supports no judgment. Unknown
        # (None) keeps the company. The judgment stamp prevents the
        # judge-eligibility backfill from re-visiting this row.
        company.eligibility_checked_at = now
        if description.founded_year and not company.year_incorporated:
            company.year_incorporated = description.founded_year
        llm_country = (description.hq_country or "").strip().upper() or None
        if llm_country:
            company.hq_country = llm_country
        if description.is_startup is False:
            company.exclusion_reason = "not_a_startup"
            company.exclusion_detail = description.not_startup_reason
            company.excluded_at = now
            summary.companies_excluded += 1
        elif llm_country is not None and llm_country != "US":
            company.exclusion_reason = "non_us"
            company.exclusion_detail = f"website states HQ country {llm_country}"
            company.excluded_at = now
            summary.companies_excluded += 1

        session.add(company)

        # People (CEO/CTO/founders) come from the same scraped pages; attribute
        # them to the company website. Replace-style so re-enrichment is clean.
        n_people = await replace_people(
            session, company.id, description.people, source_url=company.website
        )
        summary.people_written += n_people

        try:
            await session.commit()
        except (StaleDataError, IntegrityError):
            # The company was deleted mid-enrich (almost always a concurrent
            # dedup-companies merge): the row UPDATE raises StaleDataError, or the
            # people INSERT raises an FK IntegrityError. Skip it, don't crash.
            await session.rollback()
            logger.warning(
                "Company %s disappeared mid-enrich (likely a concurrent merge)"
                " — skipping.",
                company.id,
            )
            summary.llm_failures += 1
            continue
        summary.companies_enriched += 1

    return summary
