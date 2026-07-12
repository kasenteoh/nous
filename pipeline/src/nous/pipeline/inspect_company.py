"""Read-only diagnostic: dump a company's husk-relevant state by slug.

No local environment holds DATABASE_URL, so ad-hoc "why is this company a
husk?" questions can't be answered from a laptop. This is the read-only
companion to exclude_company: it prints the exact fields that decide whether a
company shows a profile, gets rescued by the scrape stage, and clears
enrichment's describe gate — the stored raw-page text lengths (the scrape
outcome), the description fields, the scrape/enrich timestamps, the failure
counter, and the prompt-version stamps. Dispatched via ops.yml against prod.

Writes nothing; safe to run any time.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanyInvestor, FundingRound, RawPage


class RawPageSummary(BaseModel):
    """One stored page: its URL, the extracted-text length, and when fetched."""

    url: str
    content_chars: int
    fetched_at: datetime | None


class CompanyInspection(BaseModel):
    """Husk-relevant snapshot of one company. All lengths, no full text dumps."""

    found: bool
    slug: str
    name: str | None = None
    website: str | None = None
    status: str | None = None
    exclusion_reason: str | None = None
    is_husk: bool | None = None  # shown (not excluded) AND description_short is null
    description_short_chars: int = 0
    description_long_chars: int = 0
    has_embedding: bool | None = None
    last_scrape_attempt_at: datetime | None = None
    website_resolved_at: datetime | None = None
    consecutive_scrape_failures: int | None = None
    last_enriched_at: datetime | None = None
    # Stamps the company_description_long prompt version (what --redescribe-outdated
    # keys on); NULL = never long-described (the husk signature).
    enrichment_prompt_version: str | None = None
    eligibility_prompt_version: str | None = None
    raw_page_count: int = 0
    max_raw_page_chars: int = 0  # the best single page — what the describe gate sees per page
    total_raw_page_chars: int = 0
    raw_pages: list[RawPageSummary] = []
    funding_round_count: int = 0
    investor_count: int = 0


async def run_inspect_company(session: AsyncSession, *, slug: str) -> CompanyInspection:
    """Load the company by slug and summarize its husk-relevant state."""
    company = (
        await session.execute(select(Company).where(Company.slug == slug))
    ).scalar_one_or_none()
    if company is None:
        return CompanyInspection(found=False, slug=slug)

    pages = list(
        (
            await session.execute(
                select(
                    RawPage.url,
                    func.length(RawPage.content).label("chars"),
                    RawPage.fetched_at,
                )
                .where(RawPage.company_id == company.id)
                .order_by(func.length(RawPage.content).desc())
            )
        ).all()
    )
    page_summaries = [
        RawPageSummary(url=url, content_chars=chars or 0, fetched_at=fetched)
        for (url, chars, fetched) in pages
    ]
    page_lengths = [p.content_chars for p in page_summaries]

    funding_count = (
        await session.execute(
            select(func.count())
            .select_from(FundingRound)
            .where(FundingRound.company_id == company.id)
        )
    ).scalar_one()
    investor_count = (
        await session.execute(
            select(func.count())
            .select_from(CompanyInvestor)
            .where(CompanyInvestor.company_id == company.id)
        )
    ).scalar_one()

    short = company.description_short or ""
    long_ = company.description_long or ""
    return CompanyInspection(
        found=True,
        slug=company.slug,
        name=company.name,
        website=company.website,
        status=company.status,
        exclusion_reason=company.exclusion_reason,
        is_husk=(company.exclusion_reason is None and not short),
        description_short_chars=len(short),
        description_long_chars=len(long_),
        has_embedding=company.embedding is not None,
        last_scrape_attempt_at=company.last_scrape_attempt_at,
        website_resolved_at=company.website_resolved_at,
        consecutive_scrape_failures=company.consecutive_scrape_failures,
        last_enriched_at=company.last_enriched_at,
        enrichment_prompt_version=company.enrichment_prompt_version,
        eligibility_prompt_version=company.eligibility_prompt_version,
        raw_page_count=len(page_summaries),
        max_raw_page_chars=max(page_lengths, default=0),
        total_raw_page_chars=sum(page_lengths),
        raw_pages=page_summaries,
        funding_round_count=funding_count,
        investor_count=investor_count,
    )
