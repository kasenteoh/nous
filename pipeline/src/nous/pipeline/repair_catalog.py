"""repair-catalog pipeline stage — one-time data repair, idempotent.

Three repairs (spec 2026-06-12 §3; placeholder-name guard added 2026-06-13):

1. Lightspeed badge-suffix names ("...LSVP and LSIP Investment" /
   "...LSIP Investment", 96 prod rows): strip the suffix; LSIP-only rows are
   Lightspeed-India holdings (out of scope) — DELETE when they are husks
   (no funding rounds, no news), soft-exclude as 'non_us' when they have
   accrued data. Renames that collide with an existing clean-named row merge
   into it via the dedup machinery.

2. Parked-domain enrichments (~30 prod rows): rows whose description matches
   conservative domain-sale prose patterns get their website + descriptions
   cleared, the bad URL recorded in rejected_urls, and their raw_pages
   dropped, so resolve/scrape/enrich start over cleanly.

3. Placeholder names (e.g. "[untitled]", empty): rows whose name matches
   ``^\\[.*\\]$`` or is empty after stripping. Repair strategy:
   - If the row has a website, derive a display name from its domain apex
     (e.g. "untitled.stream" → "Untitled"). Re-slug and re-normalize in place.
   - If no website and no derivable name, soft-exclude as 'manual' so the row
     leaves all catalog listings without being deleted (preserves any accrued
     data for future manual resolution).
   The adapters now reject these entries at ingest time, so Pass 3 is a
   one-shot back-fill for the single prod row that already exists.

Idempotent: pass 1 leaves no suffixed names; pass 2 clears the descriptions
it matches on; pass 3 renames/excludes placeholder rows. A second run selects
nothing. ``--dry-run`` logs intended actions without writing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle, RawPage

# Reuse upsert's slug/lookup helpers — underscored there, but deliberately
# shared with this one-time stage rather than duplicated. A future refactor of
# upsert.py should know these names have an external caller.
from nous.db.upsert import _build_slug, _find_by_normalized_name, merge_companies
from nous.sources.vc_portfolios.base import is_placeholder_name
from nous.util.slugify import normalize_name
from nous.util.url import hostname

logger = logging.getLogger(__name__)

_BOTH_SUFFIX = "LSVP and LSIP Investment"
_LSIP_SUFFIX = "LSIP Investment"

# Conservative domain-sale prose patterns (matched against description_short).
# Deliberately requires domain-sale wording — bare "for sale" false-matched
# real product copy (SellRaze) in the prod analysis. Rows these miss (wrong
# but live sites, launching-soon pages) are left for judge-eligibility /
# manual exclusion; see the spec's repair section.
_PARKED_DESC_PATTERNS: tuple[str, ...] = (
    "%domain%for sale%",
    "%for sale%domain%",
    "%parking page%",
    "%parked%",
    "%domain marketplace%",
    "%placeholder%for sale%",
)


class RepairSummary(BaseModel):
    names_cleaned: int = 0
    merged: int = 0
    lsip_deleted: int = 0
    lsip_excluded: int = 0
    parked_reset: int = 0
    placeholder_renamed: int = 0
    placeholder_excluded: int = 0
    dry_run: bool = False


def _domain_to_display_name(website: str) -> str | None:
    """Derive a human-readable company name from a website domain.

    Strategy: strip ``www.``, take the part before the first dot (the apex/
    SLD label), title-case it.  Returns None when the result is empty or would
    itself be a placeholder.

    Examples:
        "https://untitled.stream/"  → "Untitled"
        "https://www.acme.io/"     → "Acme"
        "https://sub.acme.co.uk/"  → "Sub"   (takes only the leftmost label)
    """
    host = hostname(website)  # strips www., lowercases
    if not host:
        return None
    apex_label = host.split(".")[0]
    if not apex_label:
        return None
    candidate = apex_label.replace("-", " ").replace("_", " ").title()
    # If the derived name is itself a placeholder, return None so we fall
    # through to the soft-exclude path.
    if is_placeholder_name(candidate):
        return None
    return candidate


async def _company_has_funding(session: AsyncSession, company_id: UUID) -> bool:
    """True when *company_id* has at least one funding round."""
    row = (
        await session.execute(
            select(FundingRound.id)
            .where(FundingRound.company_id == company_id)
            .limit(1)
        )
    ).first()
    return row is not None


async def _company_has_news(session: AsyncSession, company_id: UUID) -> bool:
    """True when *company_id* has at least one news article."""
    row = (
        await session.execute(
            select(NewsArticle.id)
            .where(NewsArticle.company_id == company_id)
            .limit(1)
        )
    ).first()
    return row is not None


async def run_repair_catalog(
    session: AsyncSession, *, dry_run: bool = False
) -> RepairSummary:
    summary = RepairSummary(dry_run=dry_run)
    now = datetime.now(tz=UTC)

    # ── Pass 1: Lightspeed badge suffixes ────────────────────────────────────
    # A single LIKE catches BOTH shapes: dual-fund rows end "...LSVP and LSIP
    # Investment", which itself ends in "LSIP Investment", so the one suffix
    # predicate matches both them and the India-only "...LSIP Investment" rows.
    # The in-loop ``is_both`` check then splits keep-and-rename from delete/exclude.
    suffixed = (
        (
            await session.execute(
                select(Company).where(Company.name.like(f"%{_LSIP_SUFFIX}"))
            )
        )
        .scalars()
        .all()
    )

    for company in suffixed:
        is_both = company.name.endswith(_BOTH_SUFFIX)
        suffix = _BOTH_SUFFIX if is_both else _LSIP_SUFFIX
        clean_name = company.name.removesuffix(suffix).strip()

        if not clean_name or not is_both:
            # LSIP-only (or a name that is nothing but the badge): India
            # portfolio — out of scope. Delete husks; the fixed adapter never
            # re-emits them. Keep + exclude rows that accrued real data.
            has_data = await _company_has_funding(
                session, company.id
            ) or await _company_has_news(session, company.id)
            if not has_data:
                logger.info("repair: deleting LSIP husk %r", company.name)
                summary.lsip_deleted += 1
                if not dry_run:
                    await session.delete(company)
                continue
            logger.info("repair: excluding LSIP row with data %r", company.name)
            summary.lsip_excluded += 1
            if not dry_run and clean_name:
                await _rename(session, company, clean_name)
            if not dry_run:
                company.exclusion_reason = "non_us"
                company.exclusion_detail = "Lightspeed India portfolio entry"
                company.excluded_at = now
                session.add(company)
            continue

        # Both-funds row: keep, clean the name; merge on collision.
        existing = await _find_by_normalized_name(session, normalize_name(clean_name))
        if existing is not None and existing.id != company.id:
            logger.info(
                "repair: merging %r into existing %r", company.name, existing.name
            )
            summary.merged += 1
            if not dry_run:
                await merge_companies(
                    session, survivor_id=existing.id, loser_id=company.id
                )
            continue

        logger.info("repair: renaming %r -> %r", company.name, clean_name)
        summary.names_cleaned += 1
        if not dry_run:
            await _rename(session, company, clean_name)
            session.add(company)

    if not dry_run:
        await session.commit()

    # ── Pass 2: parked-domain enrichments ────────────────────────────────────
    parked = (
        (
            await session.execute(
                select(Company).where(
                    Company.website.is_not(None),
                    or_(
                        *[
                            Company.description_short.ilike(p)
                            for p in _PARKED_DESC_PATTERNS
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    for company in parked:
        logger.info(
            "repair: resetting parked row %r (website %s; desc %r)",
            company.name,
            company.website,
            (company.description_short or "")[:80],
        )
        summary.parked_reset += 1
        if dry_run:
            continue
        if company.website:
            company.rejected_urls = [*(company.rejected_urls or []), company.website]
        company.website = None
        company.website_resolved_at = None
        company.description_short = None
        company.description_long = None
        company.primary_category = None
        company.tags = None
        company.last_enriched_at = None
        company.last_enriched_payload = None
        company.eligibility_checked_at = None
        await session.execute(
            delete(RawPage).where(RawPage.company_id == company.id)
        )
        session.add(company)

    if not dry_run:
        await session.commit()

    # ── Pass 3: placeholder company names ────────────────────────────────────
    # Select rows whose name matches ^\[.*\]$ or is empty / whitespace-only.
    # The LIKE '%[%]%' pre-filter is an indexed approximation; the is_placeholder_name
    # Python check is the authoritative gate so false-positives (e.g. "Acme [NY]")
    # are never touched.
    #
    # We use two separate queries (bracketed vs. empty-ish) and union their
    # results in Python so the SQL stays readable and doesn't require a regex
    # extension.  Both patterns are cheap exact/LIKE lookups.
    bracketed_rows = (
        (
            await session.execute(
                select(Company).where(Company.name.like("[%]"))
            )
        )
        .scalars()
        .all()
    )
    # Empty / whitespace-only names are pathological but defend against them.
    empty_rows = (
        (
            await session.execute(
                select(Company).where(Company.name == "")
            )
        )
        .scalars()
        .all()
    )

    placeholder_rows: list[Company] = [
        c
        for c in {*bracketed_rows, *empty_rows}
        if is_placeholder_name(c.name)
    ]

    for company in placeholder_rows:
        derived_name: str | None = None
        if company.website:
            derived_name = _domain_to_display_name(company.website)

        if derived_name:
            logger.info(
                "repair: renaming placeholder %r -> %r (website %s)",
                company.name,
                derived_name,
                company.website,
            )
            summary.placeholder_renamed += 1
            if not dry_run:
                await _rename(session, company, derived_name)
                session.add(company)
        else:
            # No website or no derivable name — soft-exclude so the row
            # disappears from all catalog views without losing accrued data.
            logger.info(
                "repair: soft-excluding un-nameable placeholder %r (website %s)",
                company.name,
                company.website,
            )
            summary.placeholder_excluded += 1
            if not dry_run:
                company.exclusion_reason = "manual"
                company.exclusion_detail = (
                    "Placeholder company name — no usable name derivable; "
                    "manually review and rename or delete."
                )
                company.excluded_at = now
                session.add(company)

    if not dry_run:
        await session.commit()

    return summary


async def _rename(session: AsyncSession, company: Company, clean_name: str) -> None:
    """Apply a cleaned display name + regenerated identity fields in place."""
    company.name = clean_name
    company.normalized_name = normalize_name(clean_name)
    company.slug = await _build_slug(
        session, clean_name, company.id, company.website
    )
