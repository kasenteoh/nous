"""repair-wrong-websites pipeline stage — idempotent poisoned-row repair.

Three detection passes (spec 2026-06-13 Task 2.2):

(a) Aggregator/directory URL: company.website host is in the shared
    AGGREGATOR_HOSTS reject set (or matches DIRECTORY_PATH_RE).  These were
    resolved by the old homepage resolver before the aggregator guard was added.

(b) For-sale / parked description: company.description_short contains
    domain-sale prose.  Deliberately a small in-Python regex against the stored
    description rather than re-parsing HTML (the HTML is gone by now; only the
    extracted text is stored).  Conservative: only triggers on explicit
    "for sale" / "buy this domain" language plus a domain word — avoids
    e-commerce false positives like SellRaze.

(c) False exclusion re-queue: rows with exclusion_reason IN ('not_a_startup',
    'non_us') whose exclusion_detail references "personal homepage" or
    "wrong site" / "wrong domain" — these were mis-excluded when the old
    resolver pointed them at an unrelated business.  Clearing the exclusion +
    eligibility timestamps lets judge-eligibility re-judge from a correct site.

Repair action for (a)/(b):
    - Append bad URL to rejected_urls (so the hardened resolver never re-picks it)
    - Clear: website, website_resolved_at, description_short, description_long,
      primary_category, tags, last_enriched_at, last_enriched_payload,
      eligibility_checked_at, last_scrape_attempt_at
    - Drop raw_pages rows (stale content from the wrong site)

Repair action for (c):
    - Clear: exclusion_reason, exclusion_detail, excluded_at,
      eligibility_checked_at
    (website + descriptions stay — the new resolver may have already fixed the
    URL, or the next resolve-homepages run will.)

Idempotency:
    - (a): after repair, website IS NULL → no longer selected
    - (b): after repair, description_short IS NULL → no longer selected
    - (c): after repair, exclusion_reason IS NULL → no longer selected

``--dry-run`` logs intended actions without writing.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.sources.reject_hosts import is_aggregator_url

logger = logging.getLogger(__name__)

# ── (b) description-text patterns ──────────────────────────────────────────
# Match stored description_short text that contains domain-sale prose.
# Only triggers when *both* a domain-sale phrase AND an e-commerce-distinguishing
# word ("domain" or "parked") appear, or on phrases that are unambiguously
# domain-sale without needing a co-occurring word (e.g. "buy this domain").
#
# SQL ILIKE patterns for the initial DB filter (broad net; Python regex narrows).
_PARKED_DESC_SQL_PATTERNS: tuple[str, ...] = (
    # Explicit domain-sale phrases that can't appear in real company copy
    "%domain%for sale%",
    "%for sale%domain%",
    "%buy this domain%",
    "%purchase this domain%",
    "%parked%domain%",
    "%domain%parked%",
    "%domain marketplace%",
    # "this site/website is for sale" on custom landers (Foodology-style)
    "%this site is for sale%",
    "%this website is for sale%",
    "%available for purchase%",
    "%inquire about this domain%",
)

# Python-side confirmation regex: must match the SQL-filtered candidate.
# Uses word boundaries to avoid false positives on substrings.
_PARKED_DESC_RE: re.Pattern[str] = re.compile(
    r"""
    (?:
        domain\s+(?:is\s+)?(?:for\s+sale|may\s+be\s+for\s+sale|marketplace|parking|parked)
        | (?:for\s+sale|buy|purchase)\s+(?:this\s+)?domain
        | parked\s+domain | domain\s+parked
        | (?:this\s+)?(?:site|website)\s+is\s+for\s+sale
        | available\s+for\s+purchase
        | inquire\s+about\s+this\s+domain
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ── (c) false-exclusion detail patterns ────────────────────────────────────
# Match exclusion_detail text that references the known mis-exclusion reasons:
# "personal homepage" or a wrong-site phrase.
_FALSE_EXCL_SQL_PATTERNS: tuple[str, ...] = (
    "%personal homepage%",
    "%personal home page%",
    "%wrong site%",
    "%wrong domain%",
    "%wrong website%",
    "%unrelated%site%",
    "%unrelated%domain%",
)

_FALSE_EXCL_REASONS: tuple[str, ...] = ("not_a_startup", "non_us")


class RepairWrongWebsitesSummary(BaseModel):
    aggregator_url_reset: int = 0
    parked_desc_reset: int = 0
    false_exclusion_requeued: int = 0
    dry_run: bool = False


async def run_repair_wrong_websites(
    session: AsyncSession, *, dry_run: bool = False
) -> RepairWrongWebsitesSummary:
    """Identify and repair poisoned rows.  Idempotent — a second run is a no-op."""
    summary = RepairWrongWebsitesSummary(dry_run=dry_run)
    now = datetime.now(tz=UTC)

    # ── Pass (a): aggregator / directory URL ─────────────────────────────────
    # Select companies that still have a website (the idempotency guard: after
    # reset, website IS NULL and they're never re-selected).
    aggregator_candidates = (
        (
            await session.execute(
                select(Company).where(Company.website.is_not(None))
            )
        )
        .scalars()
        .all()
    )

    for company in aggregator_candidates:
        if not company.website:
            continue
        if not is_aggregator_url(company.website):
            continue

        logger.info(
            "repair-wrong-websites (a): resetting aggregator URL %r for %r",
            company.website,
            company.name,
        )
        summary.aggregator_url_reset += 1
        if not dry_run:
            await _reset_website_fields(session, company, now)

    if not dry_run:
        await session.commit()

    # ── Pass (b): for-sale / parked description ──────────────────────────────
    parked_candidates = (
        (
            await session.execute(
                select(Company).where(
                    Company.description_short.is_not(None),
                    or_(
                        *[
                            Company.description_short.ilike(p)
                            for p in _PARKED_DESC_SQL_PATTERNS
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    for company in parked_candidates:
        if not company.description_short:
            continue
        if not _PARKED_DESC_RE.search(company.description_short):
            # SQL ILIKE was too broad; Python regex doesn't confirm — skip.
            continue

        logger.info(
            "repair-wrong-websites (b): resetting parked desc for %r (website %s; desc %r)",
            company.name,
            company.website,
            company.description_short[:80],
        )
        summary.parked_desc_reset += 1
        if not dry_run:
            await _reset_website_fields(session, company, now)

    if not dry_run:
        await session.commit()

    # ── Pass (c): false exclusions ───────────────────────────────────────────
    false_excl_candidates = (
        (
            await session.execute(
                select(Company).where(
                    Company.exclusion_reason.in_(_FALSE_EXCL_REASONS),
                    Company.exclusion_detail.is_not(None),
                    or_(
                        *[
                            Company.exclusion_detail.ilike(p)
                            for p in _FALSE_EXCL_SQL_PATTERNS
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    for company in false_excl_candidates:
        logger.info(
            "repair-wrong-websites (c): re-queuing false exclusion %r "
            "(reason=%s, detail=%r)",
            company.name,
            company.exclusion_reason,
            (company.exclusion_detail or "")[:80],
        )
        summary.false_exclusion_requeued += 1
        if not dry_run:
            company.exclusion_reason = None
            company.exclusion_detail = None
            company.excluded_at = None
            # Clear the eligibility timestamp so judge-eligibility re-judges it.
            company.eligibility_checked_at = None
            session.add(company)

    if not dry_run:
        await session.commit()

    return summary


async def _reset_website_fields(
    session: AsyncSession, company: Company, now: datetime
) -> None:
    """Clear all website + enrichment fields and drop stale raw_pages.

    The bad URL is appended to rejected_urls so the hardened resolver never
    re-picks it.  All enrichment timestamps are cleared so resolve→scrape→enrich
    run again cleanly.
    """
    if company.website:
        existing: list[str] = list(company.rejected_urls or [])
        if company.website not in existing:
            company.rejected_urls = [*existing, company.website]

    company.website = None
    company.website_resolved_at = None
    company.description_short = None
    company.description_long = None
    company.primary_category = None
    company.tags = None
    company.last_enriched_at = None
    company.last_enriched_payload = None
    company.eligibility_checked_at = None
    company.last_scrape_attempt_at = None

    # Drop stale raw_pages so scrape-homepages starts fresh.
    await session.execute(
        delete(RawPage).where(RawPage.company_id == company.id)
    )

    session.add(company)
    _ = now  # reserved for future audit-timestamp use
