"""Idempotent upsert helpers for company discovery + funding ingestion.

All functions operate on an open ``AsyncSession``.  Callers are responsible
for committing.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    CompanyInvestor,
    CompanyRelationship,
    Competitor,
    FundingRound,
    FundingRoundInvestor,
    Investor,
    NewsArticle,
    Person,
    RawPage,
)
from nous.llm.prompts.company_description import PersonExtraction
from nous.llm.prompts.funding_extraction import FundingExtraction
from nous.util.investor_name import canonicalize_investor_name
from nous.util.slugify import normalize_name, slug_with_disambiguator, slugify
from nous.util.url import canonical_domain


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
    session: AsyncSession, name: str, company_id: UUID | None, website: str | None = None
) -> str:
    """Generate a unique slug for *name*, appending a disambiguator if needed.

    The disambiguator seed is ``name + (website or "")``, making it
    deterministic: re-creating the same company produces the same slug.

    Edge-case: names whose normalized form is empty (e.g. all-symbol names like
    "!!!" or "---") all fall back to base="company" and bypass
    find_company_by_name (which returns None for empty norm). When website is
    also None, every such name produces the same seed "" → the same final slug
    → IntegrityError on the second insert.  We detect this by checking the
    disambiguated candidate and extending the hash suffix deterministically
    (counter-appended to the seed) until a free slot is found.
    """
    base = slugify(name)
    if not base:
        # Fallback: use a disambiguator on an empty base slug to avoid ''
        base = "company"
    candidate = base
    if await _is_slug_taken(session, candidate, exclude_id=company_id):
        seed = name + (website or "")
        candidate = slug_with_disambiguator(base, seed)
        # If the deterministic candidate is also taken (see norm-empty edge case
        # in the docstring), keep extending by hashing seed+counter until free.
        counter = 1
        while await _is_slug_taken(session, candidate, exclude_id=company_id):
            candidate = slug_with_disambiguator(base, seed + str(counter))
            counter += 1
    return candidate


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


async def replace_people(
    session: AsyncSession,
    company_id: UUID,
    people: list[PersonExtraction],
    *,
    source_url: str | None,
) -> int:
    """Replace the leadership/founder rows for a company.

    DELETEs existing People rows for *company_id*, then INSERTs the new set in
    list order (rank = 1-based position). Idempotent: calling twice with the
    same *people* yields the same final state. Names are de-duplicated
    (case-insensitive) preserving first-seen order, so the same person listed
    twice on a site doesn't violate the (company_id, rank) layout.

    Returns the number of rows inserted.
    """
    await session.execute(delete(Person).where(Person.company_id == company_id))

    seen: set[str] = set()
    rows: list[Person] = []
    for person in people:
        name = person.name.strip()
        role = person.role.strip()
        if not name or not role:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            Person(
                company_id=company_id,
                name=name,
                role=role,
                source_url=source_url,
                rank=len(rows) + 1,
            )
        )

    if rows:
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

    Short-name guard: names whose normalized form is fewer than 6 characters
    never enter the trigram branch (trigram similarity is unreliable for very
    short strings — "ai", "vue", "x" can score above 0.85 against unrelated
    companies). Exact matches are unaffected by this guard and are always
    returned regardless of length.

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

    # Trigram similarity is unreliable for very short normalized strings:
    # "ai", "vue", "x" match unrelated companies at 0.85. Skip the fuzzy
    # branch entirely when the key is shorter than 6 chars.
    if len(norm) < 6:
        return None

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
    - hq_country defaults to "US"
    - slug is built via _build_slug, with disambiguation via a deterministic
      sha256-seeded suffix (first 6 hex chars of sha256(name + website)) when
      the base slug is already taken
    - description_short stays NULL — M2's enrich-companies stage will fill
      it from the scraped homepage, which is more authoritative than any
      VC-portfolio one-liner.
    """
    # Domain dedup first — a shared canonical website domain is a far stronger
    # identity signal than a fuzzy name match, and stops the duplicate before
    # two rows are ever created (shared-hosting domains never match). Fall back
    # to name matching when there's no website or no domain hit.
    existing = await find_company_by_domain(session, website)
    if existing is None:
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
    slug = await _build_slug(session, name, None, website)
    company = Company(
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


async def refresh_funding_round_count(
    session: AsyncSession, company_id: UUID
) -> None:
    """Recompute companies.funding_round_count from funding_rounds.

    Set-based and idempotent — safe to call after any round insert or merge.
    The denormalized count exists for the web catalog bar (see migration 0022).
    """
    cnt = (
        select(func.count())
        .select_from(FundingRound)
        .where(FundingRound.company_id == company_id)
        .scalar_subquery()
    )
    await session.execute(
        update(Company)
        .where(Company.id == company_id)
        .values(funding_round_count=cnt)
    )


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
    await refresh_funding_round_count(session, company_id)
    return new_round, True


async def _is_investor_slug_taken(session: AsyncSession, slug: str) -> bool:
    """Return True if *slug* is already in use by an investor row."""
    result = await session.execute(select(Investor.id).where(Investor.slug == slug))
    return result.scalar_one_or_none() is not None


async def build_investor_slug(
    session: AsyncSession, *, name: str, name_normalized: str
) -> str:
    """Generate a unique slug for an investor, deterministically disambiguated.

    The base slug comes from ``slugify(name)``. Disambiguation is seeded by the
    investor's unique ``name_normalized`` (NOT os.urandom or the display name),
    so the same investor always resolves to the same slug — the in-migration
    backfill and the live insert path agree, and re-runs are stable.

    Mirrors :func:`_build_slug` for companies. The empty-base fallback (names
    whose slug is "", e.g. all-symbol firms) uses ``"investor"`` so the route
    never collapses to ``/investor/``; collisions there are still resolved by
    the name_normalized-seeded suffix.
    """
    base = slugify(name)
    if not base:
        base = "investor"
    candidate = base
    if await _is_investor_slug_taken(session, candidate):
        candidate = slug_with_disambiguator(base, name_normalized)
        # If the deterministic candidate is itself taken (distinct firms whose
        # names slugify identically AND whose seeds hash-collide, or the
        # empty-base fallback), keep extending by hashing seed+counter until free.
        counter = 1
        while await _is_investor_slug_taken(session, candidate):
            candidate = slug_with_disambiguator(base, name_normalized + str(counter))
            counter += 1
    return candidate


async def upsert_investor(
    session: AsyncSession, *, name: str
) -> tuple[Investor, bool]:
    """Find or create an Investor by canonicalized name.

    Display name (preserved on ``Investor.name``) keeps the first-seen casing;
    re-using an existing row does not rewrite the display name even if a later
    article uses a different casing.

    New rows get a URL slug via :func:`build_investor_slug`, with deterministic
    collision handling seeded by ``name_normalized`` — so the same investor
    always lands on the same slug.

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

    display_name = name.strip()
    slug = await build_investor_slug(
        session, name=display_name, name_normalized=canonical
    )
    investor = Investor(name=display_name, name_normalized=canonical, slug=slug)
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


# ---------------------------------------------------------------------------
# Company-level investor link (used by refresh-vc-portfolios)
# ---------------------------------------------------------------------------


async def link_company_investor(
    session: AsyncSession,
    *,
    company_id: UUID,
    investor_id: UUID,
    source: str,
    is_lead: bool = False,
) -> None:
    """Upsert a company-level (company, investor) link.

    Sticky ``is_lead``: once True it stays True even if a later signal omits
    the lead distinction — same rationale as :func:`link_round_investor`.
    ``source`` records how we learned of the investment (e.g. 'vc_portfolio')
    and is left untouched on conflict so the first-recorded source wins.

    Implemented via INSERT ... ON CONFLICT DO UPDATE on the (company_id,
    investor_id) unique constraint, so re-running the discovering stage never
    duplicates the link.
    """
    stmt = (
        pg_insert(CompanyInvestor)
        .values(
            company_id=company_id,
            investor_id=investor_id,
            source=source,
            is_lead=is_lead,
        )
        .on_conflict_do_update(
            constraint="uq_company_investors_company_investor",
            set_={
                "is_lead": CompanyInvestor.is_lead.op("OR")(is_lead),
            },
        )
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Company de-duplication: domain match + merge primitive (used by dedup-companies)
# ---------------------------------------------------------------------------


# Company scalar/array/jsonb columns that merge_companies fills on the survivor
# from the loser when the survivor's value is NULL (one-directional gap-fill).
_MERGE_FILL_COLUMNS: tuple[str, ...] = (
    "website",
    "logo_url",
    "description_short",
    "description_long",
    "primary_category",
    "tags",
    "hq_city",
    "hq_state",
    "hq_country",
    "industry_group",
    "year_incorporated",
    "last_enriched_at",
    "last_enriched_payload",
    "website_resolved_at",
)


async def find_company_by_domain(
    session: AsyncSession, website: str | None
) -> Company | None:
    """Find an existing Company that shares ``website``'s canonical domain.

    Returns None when ``website`` is None/empty or its host is a shared-hosting
    domain (``canonical_domain`` returns None) — those carry no identity signal,
    so we must not collapse rows on them.

    Two stages:
    1. A cheap, index-assisted prefilter: ``website ILIKE %domain%`` narrows the
       candidate set without normalizing every row in SQL.
    2. An exact host check in Python: keep the first candidate whose own
       ``canonical_domain(website)`` equals ``domain``. The ILIKE can over-match
       (e.g. ``domain='acme.com'`` would also catch ``notacme.com`` or a path
       segment), so the normalized equality is what makes the match correct.
    """
    domain = canonical_domain(website)
    if domain is None:
        return None

    stmt = select(Company).where(
        Company.website.is_not(None),
        Company.website.ilike(f"%{domain}%"),
    )
    result = await session.execute(stmt)
    for candidate in result.scalars():
        if canonical_domain(candidate.website) == domain:
            return candidate
    return None


async def merge_companies(
    session: AsyncSession, *, survivor_id: UUID, loser_id: UUID
) -> None:
    """Fold the ``loser`` company into ``survivor``, then delete the loser.

    Every child row that references ``loser_id`` is repointed to ``survivor_id``,
    handling each table's unique constraints so no IntegrityError can occur:

    - **raw_pages** — unique (company_id, url): move loser rows whose url the
      survivor lacks; delete the rest (survivor already has that url).
    - **news_articles** — url is globally unique (no per-company constraint):
      blanket repoint company_id.
    - **funding_rounds** — no unique beyond the pk: blanket repoint, then the
      survivor's ``funding_round_count`` is recomputed. Their
      ``funding_round_investors`` hang off funding_round_id and follow along.
    - **company_investors** — unique (company_id, investor_id): move links the
      survivor lacks; delete loser links the survivor already has (OR-promoting
      is_lead first).
    - **competitors.company_id** — unique (company_id, rank): DELETE the loser's
      rows outright (regenerated by analyze-competitors; avoids rank collisions).
    - **competitors.competitor_company_id** — nullable FK: drop rows that would
      become self-references, repoint the rest to survivor, then de-duplicate
      the resulting pairs.
    - **people** — unique (company_id, rank): adopt the loser's people only when
      the survivor has none (enrich is write-once); otherwise the survivor's win.
    - **company_relationships** — derived edges: drop every edge touching the
      loser (either direction); derive-relationships rebuilds the survivor's set
      on its next run (it follows dedup in discovery.yml).

    The survivor's NULL scalar/array/jsonb fields are then filled from the loser
    (see :data:`_MERGE_FILL_COLUMNS`) — a one-directional "fill the gaps" so we
    keep whatever the survivor already had and only borrow what it was missing.

    Finally the loser row is deleted. This function does NOT commit — the caller
    owns the transaction. It is a one-way fold: after it runs, ``loser_id`` no
    longer exists, so a re-run cannot double-apply.
    """
    if survivor_id == loser_id:
        raise ValueError("merge_companies: survivor_id and loser_id are identical")

    # --- raw_pages: unique (company_id, url) --------------------------------
    survivor_urls_subq = select(RawPage.url).where(RawPage.company_id == survivor_id)
    # Delete loser rows whose url the survivor already has.
    await session.execute(
        delete(RawPage).where(
            RawPage.company_id == loser_id,
            RawPage.url.in_(survivor_urls_subq),
        )
    )
    # Move the remaining loser rows (urls the survivor lacks).
    await session.execute(
        update(RawPage)
        .where(RawPage.company_id == loser_id)
        .values(company_id=survivor_id)
    )

    # --- news_articles: url globally unique, no per-company constraint ------
    await session.execute(
        update(NewsArticle)
        .where(NewsArticle.company_id == loser_id)
        .values(company_id=survivor_id)
    )

    # --- funding_rounds: no unique beyond pk -------------------------------
    await session.execute(
        update(FundingRound)
        .where(FundingRound.company_id == loser_id)
        .values(company_id=survivor_id)
    )
    # Keep the denormalized catalog-bar count truthful for the survivor.
    await refresh_funding_round_count(session, survivor_id)

    # --- company_investors: unique (company_id, investor_id) ---------------
    survivor_investors_subq = select(CompanyInvestor.investor_id).where(
        CompanyInvestor.company_id == survivor_id
    )
    # Preserve sticky is_lead: if the loser flags a shared investor as lead,
    # promote the survivor's link to lead before dropping the loser's duplicate.
    loser_lead_subq = select(CompanyInvestor.investor_id).where(
        CompanyInvestor.company_id == loser_id,
        CompanyInvestor.is_lead.is_(True),
    )
    await session.execute(
        update(CompanyInvestor)
        .where(
            CompanyInvestor.company_id == survivor_id,
            CompanyInvestor.investor_id.in_(loser_lead_subq),
        )
        .values(is_lead=True)
    )
    await session.execute(
        delete(CompanyInvestor).where(
            CompanyInvestor.company_id == loser_id,
            CompanyInvestor.investor_id.in_(survivor_investors_subq),
        )
    )
    await session.execute(
        update(CompanyInvestor)
        .where(CompanyInvestor.company_id == loser_id)
        .values(company_id=survivor_id)
    )

    # --- competitors.company_id: drop the loser's ranked set ----------------
    await session.execute(
        delete(Competitor).where(Competitor.company_id == loser_id)
    )

    # --- competitors.competitor_company_id: clean up, then repoint ----------
    # Drop rows that *would* become self-references after the repoint (the
    # survivor listing the loser as its competitor). Deleting BEFORE the
    # repoint keeps the ck_competitors_no_self_reference CHECK satisfied at
    # every step — repointing first would transiently violate it.
    await session.execute(
        delete(Competitor).where(
            Competitor.company_id == survivor_id,
            Competitor.competitor_company_id == loser_id,
        )
    )
    await session.execute(
        update(Competitor)
        .where(Competitor.competitor_company_id == loser_id)
        .values(competitor_company_id=survivor_id)
    )
    # De-duplicate any (company_id, competitor_company_id) pairs the repoint
    # created — keep the lowest-id row per pair, delete the rest.
    dup_self = Competitor.__table__.alias("dup_self")
    dup_other = Competitor.__table__.alias("dup_other")
    duplicate_ids_subq = (
        select(dup_self.c.id)
        .select_from(dup_self.join(
            dup_other,
            and_(
                dup_self.c.company_id == dup_other.c.company_id,
                dup_self.c.competitor_company_id == dup_other.c.competitor_company_id,
                dup_self.c.competitor_company_id.is_not(None),
                dup_self.c.id > dup_other.c.id,
            ),
        ))
    )
    await session.execute(
        delete(Competitor).where(
            Competitor.id.in_(duplicate_ids_subq)
        )
    )

    # --- company_relationships: derived, regenerated edges -----------------
    # These are recomputed wholesale by derive-relationships, which runs right
    # after dedup-companies in discovery.yml. Drop every edge touching the loser
    # in EITHER direction; the survivor's full set is rebuilt on the next derive
    # run. Repointing instead would risk unique-triple collisions (and transient
    # self-edge CHECK violations) for zero benefit on regenerated data — the same
    # reason company_snapshots is left to CASCADE.
    await session.execute(
        delete(CompanyRelationship).where(
            or_(
                CompanyRelationship.company_id == loser_id,
                CompanyRelationship.related_company_id == loser_id,
            )
        )
    )

    # --- people: unique (company_id, rank) ---------------------------------
    survivor_has_people = (
        await session.execute(
            select(Person.id).where(Person.company_id == survivor_id).limit(1)
        )
    ).first() is not None
    if survivor_has_people:
        await session.execute(delete(Person).where(Person.company_id == loser_id))
    else:
        await session.execute(
            update(Person)
            .where(Person.company_id == loser_id)
            .values(company_id=survivor_id)
        )

    # --- fill survivor NULLs from loser ------------------------------------
    survivor = await session.get(Company, survivor_id)
    loser = await session.get(Company, loser_id)
    if survivor is None or loser is None:
        raise ValueError(
            f"merge_companies: survivor {survivor_id} or loser {loser_id} not found"
        )
    for column in _MERGE_FILL_COLUMNS:
        if getattr(survivor, column) is None:
            loser_value = getattr(loser, column)
            if loser_value is not None:
                setattr(survivor, column, loser_value)
    session.add(survivor)

    # --- delete the loser ---------------------------------------------------
    # Flush first so the FK repoints above are visible to the delete; the loser
    # now has no children pointing at it.
    await session.flush()
    await session.delete(loser)
    await session.flush()
