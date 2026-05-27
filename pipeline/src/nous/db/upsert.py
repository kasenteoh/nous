"""Idempotent upsert helpers for Form D ingestion.

All functions operate on an open ``AsyncSession``.  Callers (i.e. the
``ingest_filings`` pipeline stage) are responsible for committing.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    Filing,
    FundingRound,
    FundingRoundInvestor,
    Investor,
    RawPage,
    RelatedPerson,
)
from nous.llm.prompts.funding_extraction import FundingExtraction
from nous.sources.form_d import FormD, FormDRelatedPerson
from nous.util.investor_name import canonicalize_investor_name
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

    Fields that are NEVER overwritten on update:
    - ``name``: first-discovery wins. SEC entity names are frequently
      ALL-CAPS ("OPENAI, INC.") and would degrade a nicer "OpenAI" already
      set by an earlier VC or news ingest. Preserved as the display name.
      (The casing-upgrade path in ``auto_create_company`` handles the
      opposite case — properly-cased VC entry replacing a lowercase name.)
    - ``slug``: URL identity. Stable across ingests so external links don't
      break when a Form D amendment changes the legal name.
    - M2 enrichment: ``description_short``, ``description_long``, ``website``,
      ``logo_url``, ``employee_count_*``, ``last_enriched_at``.
    - ``discovered_via``: source provenance.

    Fields that ARE updated on every ingest (most-recent-filing wins):
    ``normalized_name``, ``hq_city``, ``hq_state``, ``hq_country``,
    ``year_incorporated``, ``industry_group``. ``normalized_name`` is the
    matching key, so it tracks the latest stylization to keep dedup working
    if the helper's rules ever change.

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
    # Only accept a name match when both rows are Form-D-sourced (the
    # original use case: an earlier no-CIK filing matched by name, current
    # filing supplies the CIK). For non-Form-D rows we fall through to
    # INSERT — accepting a possible duplicate is safer than hijacking a
    # VC/news/TC row that may represent a different real-world company.
    if company is None:
        candidate = await _find_by_normalized_name(session, norm)
        if candidate is not None:
            if not cik:
                # Without an incoming CIK, this is a re-ingest of the same
                # no-CIK Form D filing — match regardless of source.
                company = candidate
            elif not candidate.cik and candidate.discovered_via == "form_d":
                # Backfill CIK only onto a prior Form D row that lacked one.
                company = candidate
                company.cik = cik  # backfill

    # -- Step 2: update or insert --
    if company is not None:
        # name and slug are first-discovery-wins; do not overwrite them.
        # normalized_name stays in sync so future matching catches new
        # stylizations even if the canonical name was set by a non-Form-D source.
        company.normalized_name = norm
        company.hq_city = addr.city
        company.hq_state = addr.state
        company.hq_country = addr.country or "US"
        company.year_incorporated = form_d.year_of_incorporation
        company.industry_group = form_d.industry_group_type or None
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


async def upsert_raw_page(
    session: AsyncSession,
    company_id: UUID,
    url: str,
    content: str,
) -> RawPage:
    """Upsert a raw HTML page for a company.

    ON CONFLICT (company_id, url) DO UPDATE SET content, fetched_at = now().
    Uses postgresql.insert; returning RawPage.id, then re-fetches via session.get
    so the caller gets a fully-populated ORM object.
    """
    from sqlalchemy import func as sa_func

    stmt = (
        pg_insert(RawPage)
        .values(
            company_id=company_id,
            url=url,
            content=content,
            fetched_at=sa_func.now(),
        )
        .on_conflict_do_update(
            index_elements=["company_id", "url"],
            set_={
                "content": content,
                "fetched_at": sa_func.now(),
            },
        )
        .returning(RawPage.id)
    )
    result = await session.execute(stmt)
    row = result.fetchone()
    assert row is not None, "upsert_raw_page: no row returned — this is a logic bug"

    raw_page_id: UUID = row[0]
    # populate_existing=True forces a refresh from the DB so the returned object
    # reflects the freshly-upserted content, not whatever is in the identity map
    # from a prior call within the same session.
    fetched = await session.get(RawPage, raw_page_id, populate_existing=True)
    assert fetched is not None, f"RawPage {raw_page_id} missing after upsert"
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


# ---------------------------------------------------------------------------
# M3: auto-create + fuzzy match (used by VC portfolio refresh + news ingest)
# ---------------------------------------------------------------------------


async def find_company_by_name(
    session: AsyncSession,
    name: str,
    *,
    similarity_threshold: float = 0.85,
) -> Company | None:
    """Find an existing Company by name. Exact normalized match first, then
    pg_trgm trigram similarity (uses the GIN index from migration 0003).

    Returns the highest-similarity match when multiple rows clear the
    threshold; returns None when no match.

    The trigram path requires the pg_trgm extension to be installed (handled
    by migration 0003). If the extension is unavailable, the similarity()
    call will raise — callers should treat that as a deployment problem,
    not an "unknown company" signal.
    """
    norm = normalize_name(name)
    if not norm:
        return None

    exact = await _find_by_normalized_name(session, norm)
    if exact is not None:
        return exact

    similarity = func.similarity(Company.normalized_name, norm)
    stmt = (
        select(Company)
        .where(similarity >= similarity_threshold)
        .order_by(similarity.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _is_lowercase_variant_of(new: str, existing: str) -> bool:
    """True when ``existing`` is exactly the all-lowercase form of ``new``.

    Used to cross-reference casing across sources: the same company often
    appears in several VC portfolios (and news) with different casing —
    e.g. Greylock's logo alt yields ``airbnb`` while a16z yields ``Airbnb``.
    Since they dedupe to one row, we let a properly-cased name upgrade an
    all-lowercase display name regardless of which source landed first.

    The condition is intentionally strict — ``existing == new.lower()`` — so
    it only fires on pure casing differences, never swapping in a different
    (fuzzy-matched) name.
    """
    return new != existing and existing == new.lower()


async def auto_create_company(
    session: AsyncSession,
    *,
    name: str,
    website: str | None,
    discovered_via: str,
    similarity_threshold: float = 0.85,
) -> tuple[Company, bool]:
    """Find-or-create a Company from a non-Form-D source (VC portfolio, news,
    TechCrunch). Match via find_company_by_name; insert if not found.

    Returns ``(company, created)`` where ``created`` is True only on insert.

    Behavior on match:
    - If the existing row has no website but the caller passed one, fill it
      in opportunistically (never overwrite an already-resolved website).
    - If the existing display name is the all-lowercase form of the incoming
      name, upgrade it to the better-cased version (cross-source casing fix).
      The slug/normalized_name are unaffected (both already lowercased).
    - discovered_via on the existing row is left alone — first-discovery
      wins (Open Question §6 in the M3 plan).

    Behavior on insert:
    - cik is NULL (non-Form-D rows don't have a CIK)
    - hq_country defaults to "US" (consistent with the M1 Form D path)
    - slug is built via the same _build_slug helper used by upsert_company,
      with disambiguation via the os.urandom branch since cik is None
    - description_short stays NULL — M2's enrich-companies stage will fill
      it from the scraped homepage, which is more authoritative than any
      VC-portfolio one-liner.
    """
    existing = await find_company_by_name(
        session, name, similarity_threshold=similarity_threshold
    )
    if existing is not None:
        if existing.website is None and website:
            existing.website = website
            session.add(existing)
        if _is_lowercase_variant_of(name, existing.name):
            existing.name = name
            session.add(existing)
        return existing, False

    norm = normalize_name(name)
    slug = await _build_slug(session, name, None, None)
    company = Company(
        cik=None,
        name=name,
        slug=slug,
        normalized_name=norm,
        hq_country="US",
        website=website,
        discovered_via=discovered_via,
    )
    session.add(company)
    await session.flush()
    return company, True


# ---------------------------------------------------------------------------
# M3: funding round reconciliation + investor upsert (used by extract-funding)
# ---------------------------------------------------------------------------


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _is_more_confident(new: str | None, existing: str | None) -> bool:
    """True if ``new`` confidence outranks ``existing``."""
    if new is None:
        return False
    if existing is None:
        return True
    return _CONFIDENCE_RANK.get(new, -1) > _CONFIDENCE_RANK.get(existing, -1)


async def reconcile_funding_round(
    session: AsyncSession,
    *,
    company_id: UUID,
    extraction: FundingExtraction,
    primary_news_url: str,
    proximity_days: int = 60,
) -> tuple[FundingRound, bool]:
    """Find an existing FundingRound for ``company_id`` whose round_type matches
    and announced_date is within ``±proximity_days``; merge into it if found,
    otherwise insert a new row.

    Match rules (intentionally strict to avoid false merges):
    - round_type matches case-insensitively when both sides are non-None.
      Both None also matches (round of unknown type).
    - announced_date matches when both sides are non-None and within the
      window. Both None also matches. Mismatched null-ness does not match
      (one side knows the date, the other doesn't — too uncertain to merge).

    Merge behavior on match:
    - Fill nulls: amount_raised, valuation_post_money, valuation_source,
      announced_date are populated when the existing row lacks them.
    - Confidence: keep the higher (low < medium < high). Never downgrade.
    - primary_news_url: first one wins — don't overwrite. The earliest
      attribution is the most stable reference.

    Returns ``(row, created)`` where ``created`` is True on insert.
    """
    candidates_stmt = select(FundingRound).where(FundingRound.company_id == company_id)

    if extraction.round_type is not None:
        candidates_stmt = candidates_stmt.where(
            func.lower(FundingRound.round_type) == extraction.round_type.lower()
        )
    else:
        candidates_stmt = candidates_stmt.where(FundingRound.round_type.is_(None))

    if extraction.announced_date is not None:
        low = extraction.announced_date - timedelta(days=proximity_days)
        high = extraction.announced_date + timedelta(days=proximity_days)
        candidates_stmt = candidates_stmt.where(
            and_(
                FundingRound.announced_date.is_not(None),
                FundingRound.announced_date >= low,
                FundingRound.announced_date <= high,
            )
        )
    else:
        candidates_stmt = candidates_stmt.where(FundingRound.announced_date.is_(None))

    existing_result = await session.execute(candidates_stmt.limit(1))
    existing = existing_result.scalar_one_or_none()

    if existing is not None:
        if existing.amount_raised is None and extraction.amount_raised_usd is not None:
            existing.amount_raised = extraction.amount_raised_usd
        if (
            existing.valuation_post_money is None
            and extraction.valuation_post_money_usd is not None
        ):
            existing.valuation_post_money = extraction.valuation_post_money_usd
        if (
            existing.valuation_source is None
            and extraction.valuation_source is not None
        ):
            existing.valuation_source = extraction.valuation_source
        if existing.announced_date is None and extraction.announced_date is not None:
            existing.announced_date = extraction.announced_date
        if _is_more_confident(extraction.confidence, existing.extraction_confidence):
            existing.extraction_confidence = extraction.confidence
        # primary_news_url: first-write-wins; do not overwrite.
        session.add(existing)
        return existing, False

    new_round = FundingRound(
        company_id=company_id,
        round_type=extraction.round_type,
        amount_raised=extraction.amount_raised_usd,
        valuation_post_money=extraction.valuation_post_money_usd,
        valuation_source=extraction.valuation_source,
        announced_date=extraction.announced_date,
        primary_news_url=primary_news_url,
        extraction_confidence=extraction.confidence,
    )
    session.add(new_round)
    await session.flush()
    return new_round, True


async def upsert_investor(
    session: AsyncSession, *, name: str
) -> tuple[Investor, bool]:
    """Find or create an Investor by canonicalized name.

    Display name (preserved on ``Investor.name``) keeps the first-seen casing;
    re-using an existing row does not rewrite the display name even if a later
    article uses a different casing.

    Returns ``(row, created)``.
    """
    canonical = canonicalize_investor_name(name)
    if not canonical:
        raise ValueError(f"investor name canonicalizes to empty: {name!r}")

    existing_result = await session.execute(
        select(Investor).where(Investor.name_normalized == canonical)
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return existing, False

    investor = Investor(name=name.strip(), name_normalized=canonical)
    session.add(investor)
    await session.flush()
    return investor, True


async def link_round_investor(
    session: AsyncSession,
    *,
    funding_round_id: UUID,
    investor_id: UUID,
    is_lead: bool,
) -> None:
    """Upsert a (round, investor) link. Sticky `is_lead`: once True, stays True
    even if a later article lists the same investor as a participant. This
    handles the case where one article identifies the lead and another lists
    all participants without distinguishing.

    Implemented via INSERT ... ON CONFLICT DO UPDATE on the (funding_round_id,
    investor_id) unique constraint.
    """
    stmt = (
        pg_insert(FundingRoundInvestor)
        .values(
            funding_round_id=funding_round_id,
            investor_id=investor_id,
            is_lead=is_lead,
        )
        .on_conflict_do_update(
            constraint="uq_funding_round_investors_round_investor",
            set_={
                "is_lead": FundingRoundInvestor.is_lead.op("OR")(is_lead),
            },
        )
    )
    await session.execute(stmt)
