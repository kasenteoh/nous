"""Idempotent upsert helpers for Form D ingestion.

All functions operate on an open ``AsyncSession``.  Callers (i.e. the
``ingest_filings`` pipeline stage) are responsible for committing.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Filing, RelatedPerson
from nous.sources.form_d import FormD, FormDRelatedPerson
from nous.util.slugify import normalize_name, slug_with_disambiguator, slugify


async def _find_by_cik(session: AsyncSession, cik: str) -> Company | None:
    """Return the Company row matching *cik*, or None."""
    result = await session.execute(select(Company).where(Company.cik == cik))
    return result.scalar_one_or_none()


async def _find_by_normalized_name(session: AsyncSession, norm: str) -> Company | None:
    """Return the Company row matching *normalized_name*, or None."""
    result = await session.execute(
        select(Company).where(Company.normalized_name == norm)
    )
    return result.scalar_one_or_none()


async def _is_slug_taken(session: AsyncSession, slug: str, exclude_id: UUID | None) -> bool:
    """Return True if *slug* is already in use by a different company."""
    stmt = select(Company.id).where(Company.slug == slug)
    if exclude_id is not None:
        stmt = stmt.where(Company.id != exclude_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _build_slug(
    session: AsyncSession, name: str, cik: str | None, company_id: UUID | None
) -> str:
    """Generate a unique slug for *name*, appending a disambiguator if needed."""
    base = slugify(name)
    if not base:
        # Fallback: use a disambiguator on an empty base slug to avoid ''
        base = "company"
    candidate = base
    if await _is_slug_taken(session, candidate, exclude_id=company_id):
        candidate = slug_with_disambiguator(base, cik)
    return candidate


async def upsert_company(session: AsyncSession, form_d: FormD) -> tuple[Company, bool]:
    """Find or create a Company from a parsed Form D.

    Lookup strategy (in order):
    1. If ``form_d.cik`` is non-empty → query by CIK.
       - Hit: update mutable fields; if row had no CIK set it now.
       - Miss → also query by normalized_name (a previous no-CIK filing may
         have created the row; if found, backfill CIK).
    2. If ``form_d.cik`` is empty → query by normalized_name only.
    3. If still no match → insert new row.

    Fields that are NEVER overwritten on update (M2+ enrichment territory):
    ``description_short``, ``description_long``, ``website``, ``logo_url``,
    ``employee_count_min``, ``employee_count_max``, ``employee_count_source``,
    ``last_enriched_at``.

    Fields that ARE updated on every ingest (most-recent-filing wins):
    ``name``, ``normalized_name``, ``hq_city``, ``hq_state``, ``hq_country``,
    ``year_incorporated``, ``industry_group``.  (Per spec open question #6.)

    Returns:
        ``(company, created)`` where *created* is True only on a fresh insert.
    """
    cik = form_d.cik.strip() if form_d.cik else ""
    norm = normalize_name(form_d.entity_name)
    addr = form_d.principal_place_of_business

    company: Company | None = None

    # -- Step 1: try CIK lookup --
    if cik:
        company = await _find_by_cik(session, cik)

    # -- Step 1b: if CIK lookup missed, check by normalized name --
    if company is None:
        company = await _find_by_normalized_name(session, norm)
        if company is not None and cik and not company.cik:
            # Backfill CIK onto an existing name-matched row.
            company.cik = cik

    # -- Step 2: update or insert --
    if company is not None:
        # Update always-overwritten fields.
        company.name = form_d.entity_name
        company.normalized_name = norm
        company.hq_city = addr.city
        company.hq_state = addr.state
        company.hq_country = addr.country or "US"
        company.year_incorporated = form_d.year_of_incorporation
        company.industry_group = form_d.industry_group_type or None
        # Refresh slug in case name changed.
        company.slug = await _build_slug(session, form_d.entity_name, cik or None, company.id)
        session.add(company)
        return company, False

    # -- Insert new company --
    slug = await _build_slug(session, form_d.entity_name, cik or None, None)
    company = Company(
        cik=cik or None,
        name=form_d.entity_name,
        slug=slug,
        normalized_name=norm,
        hq_city=addr.city,
        hq_state=addr.state,
        hq_country=addr.country or "US",
        year_incorporated=form_d.year_of_incorporation,
        industry_group=form_d.industry_group_type or None,
    )
    session.add(company)
    # Flush so company.id is populated before callers reference it.
    await session.flush()
    return company, True


async def insert_filing_if_new(
    session: AsyncSession, company_id: UUID, form_d: FormD
) -> Filing | None:
    """Insert a Filing row, ignoring duplicates on ``accession_number``.

    Uses ``INSERT … ON CONFLICT (accession_number) DO NOTHING RETURNING id``
    so the insert is truly idempotent: a second call with the same accession
    number returns ``None`` without raising.

    Returns:
        The newly-created ``Filing`` on first insert, or ``None`` if the row
        already existed.
    """
    stmt = (
        pg_insert(Filing)
        .values(
            company_id=company_id,
            accession_number=form_d.accession_number,
            filing_date=form_d.filing_date,
            offering_amount_total=form_d.total_offering_amount,
            amount_sold=form_d.total_amount_sold,
            investors_count=form_d.total_number_already_invested,
            minimum_investment=form_d.minimum_investment_accepted,
            raw_data=form_d.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(index_elements=["accession_number"])
        .returning(Filing.id)
    )
    result = await session.execute(stmt)
    row = result.fetchone()
    if row is None:
        # Conflict — row already exists, nothing inserted.
        return None

    filing_id: UUID = row[0]
    # Re-fetch the ORM object so callers can access all columns.
    fetched = await session.get(Filing, filing_id)
    # get() returns None only if the row somehow doesn't exist — that would be
    # a logic bug, not a user error, so a hard assert is appropriate.
    assert fetched is not None, f"Filing {filing_id} missing after insert"
    return fetched


async def replace_related_persons(
    session: AsyncSession,
    company_id: UUID,
    filing_id: UUID,
    persons: list[FormDRelatedPerson],
) -> int:
    """Delete existing RelatedPerson rows for *filing_id*, then re-insert.

    Idempotent: calling with the same *persons* list twice yields the same
    final DB state.  Scoped to ``filing_id`` so multiple filings for the same
    company don't interfere with each other.

    Returns:
        Number of rows inserted.
    """
    await session.execute(
        delete(RelatedPerson).where(RelatedPerson.filing_id == filing_id)
    )
    if not persons:
        return 0

    rows = [
        RelatedPerson(
            company_id=company_id,
            filing_id=filing_id,
            name=p.name,
            relationship=p.relationship,
            address=p.address.model_dump() if p.address is not None else None,
        )
        for p in persons
    ]
    session.add_all(rows)
    return len(rows)
