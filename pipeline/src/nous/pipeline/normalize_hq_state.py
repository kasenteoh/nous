"""normalize-hq-state stage — canonicalize companies.hq_state to the USPS code.

``companies.hq_state`` accumulated in mixed forms ("California" vs "CA" vs
"ca"), and the web renders whatever casing is stored. The location route
(``web/app/location/[state]/page.tsx``) resolves ``/location/<seg>`` by
uppercasing ``<seg>`` and matching it against the stored ``hq_state``
(``q.eq("hq_state", opts.state)`` in ``web/lib/queries.ts``), so the 2-letter
UPPERCASE USPS code is the form routing already expects. This stage rewrites the
column to that canonical form (see :mod:`nous.util.us_state`).

Routing-safety: this only ever turns a NON-canonical US-state spelling into its
code. Rows already "CA" are never selected, so every ``/location/CA`` URL that
resolves today keeps resolving. Full-name rows (whose company-page
``/location/California`` link 404s today, because the route uppercases to
"CALIFORNIA" and nothing is stored that way) start pointing at the working
``/location/CA``. No previously-resolving URL changes; broken ones heal.

Self-bounding & idempotent: the SELECT filters — entirely in SQL — to rows whose
``hq_state`` is a recognized US-state spelling that is not already the uppercase
code, so ``--limit`` bounds real work and a second full run selects nothing.
Non-US / territory / garbage values never match the filter (and
``canonical_us_state`` returns None for them anyway), so they are left untouched.

One commit per row (mirrors embed-companies), so a mid-run crash leaves every
already-processed row consistent. ``StaleDataError`` (a concurrent dedup merge
deleting a row mid-run) skips the row rather than sinking the run. Records no new
source — this is a pure format normalization, no schema change.

Second pass — unevidenced-US reset. Until 2026-10-09 the enrich / judge
country tiers stamped ``hq_country='US'`` whenever ANY ``hq_state`` or
``hq_city`` was present, so a company with only "London" or "Bangalore" (no
stated country, generic .com) became "US". That stamp both skips the non_us
exclusion and hides the row from infer-hq-country (which selects
``hq_country IS NULL``). This pass resets such rows to NULL when the "US" has
no evidence behind it: ``hq_state`` is not a real US state, the stored enrich
payload states no country, and infer-hq-country never verified it. (The ccTLD
tier can never have produced "US" — generic TLDs map to nothing and .us is
not in the map — so it needs no check here.) Visibility is unchanged
(NULL-country rows stay shown); the row simply becomes eligible for
infer-hq-country's sourced judgment. A false reset
costs one infer-hq-country check, never an exclusion. Idempotent: a reset row
no longer matches (hq_country is NULL).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.sql.elements import ColumnElement

from nous.db.models import Company
from nous.util.us_state import (
    US_STATE_CODES,
    US_STATE_NAME_TO_CODE,
    canonical_us_state,
)

logger = logging.getLogger(__name__)


class NormalizeHqStateSummary(BaseModel):
    """Result of one normalize-hq-state run."""

    companies_seen: int = 0  # rows selected as needing normalization
    normalized: int = 0  # rows whose hq_state was rewritten (or WOULD be, dry-run)
    errors: int = 0  # concurrent-delete skips
    # Second pass: hq_country='US' rows re-examined / reset to NULL (or WOULD be).
    us_rows_checked: int = 0
    unevidenced_us_reset: int = 0


def _needs_normalization() -> ColumnElement[bool]:
    """SQL predicate selecting exactly the rows canonical_us_state would change.

    A row needs work iff its ``hq_state`` is a recognized US-state spelling (a
    code with case/whitespace noise, or a full name) that is NOT already the
    uppercase 2-letter code. Expressed purely in SQL so ``--limit`` bounds real
    work and non-US / garbage rows are never selected:

    - ``hq_state IS NOT NULL`` and not already one of the canonical codes; AND
    - it normalizes to a state: either ``upper(trim(hq_state))`` is a code (so
      "ca" / "CA " qualify) or ``lower(trim(hq_state))`` is a known full name.

    The per-row loop re-checks :func:`canonical_us_state` (the authority) before
    writing, so an exotic-whitespace edge where SQL ``trim`` and Python
    ``str.strip`` disagree can never write a wrong or NULL value — at worst such
    a row is skipped this run.
    """
    codes = sorted(US_STATE_CODES)
    names = sorted(US_STATE_NAME_TO_CODE)
    return and_(
        Company.hq_state.is_not(None),
        Company.hq_state.notin_(codes),
        or_(
            func.upper(func.trim(Company.hq_state)).in_(codes),
            func.lower(func.trim(Company.hq_state)).in_(names),
        ),
    )


async def run_normalize_hq_state(
    session: AsyncSession,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> NormalizeHqStateSummary:
    """Rewrite non-canonical US ``hq_state`` values to their USPS code.

    Selects only rows whose ``hq_state`` is a US-state spelling differing from
    its canonical code (see :func:`_needs_normalization`), and rewrites each to
    the code. One commit per row. ``dry_run`` logs and counts the intended
    changes without writing. Idempotent: a re-run finds no remaining
    differences (each code is a fixed point of ``canonical_us_state``).
    """
    summary = NormalizeHqStateSummary()

    stmt = select(Company).where(_needs_normalization()).order_by(Company.id)
    if limit is not None:
        stmt = stmt.limit(limit)

    companies = (await session.execute(stmt)).scalars().all()
    summary.companies_seen = len(companies)

    for company in companies:
        canon = canonical_us_state(company.hq_state)
        # Belt-and-suspenders: the SQL filter already excludes non-US and
        # already-canonical rows, but re-check in Python (the authority) so a
        # None or no-op can never slip through and write a wrong/NULL value.
        if canon is None or canon == company.hq_state:
            continue

        logger.info(
            "normalize-hq-state: %r -> %r (slug=%s)%s",
            company.hq_state,
            canon,
            company.slug,
            " [dry-run]" if dry_run else "",
        )
        if dry_run:
            summary.normalized += 1
            continue

        company.hq_state = canon
        session.add(company)
        try:
            await session.commit()
        except StaleDataError:
            # Row deleted mid-run — almost always a concurrent dedup merge.
            await session.rollback()
            logger.warning(
                "Company %s disappeared mid-normalize (likely a concurrent merge)"
                " — skipping.",
                company.id,
            )
            summary.errors += 1
            continue
        summary.normalized += 1

    await _reset_unevidenced_us(session, summary, limit=limit, dry_run=dry_run)

    logger.info(
        "normalize-hq-state: seen=%d normalized=%d errors=%d "
        "us_checked=%d unevidenced_us_reset=%d",
        summary.companies_seen,
        summary.normalized,
        summary.errors,
        summary.us_rows_checked,
        summary.unevidenced_us_reset,
    )
    return summary


def is_unevidenced_us(
    *,
    hq_state: str | None,
    hq_city: str | None,
    enriched_payload: dict[str, object] | None,
) -> bool:
    """True when a stored ``hq_country='US'`` carries the old tier-3 leak
    signature: a city or a non-US region was present (that is what fired the
    rule), there is no real US state, and the enrich LLM payload states no
    country. Pure.

    Rows with NEITHER a state nor a city are deliberately out of scope: the
    tier-3 rule could not have produced their "US" (it needed a state or a
    city), so it came from another path — typically an explicit
    judge-eligibility verdict, which is not stored in the enrich payload. The
    first prod run (2026-10-09) used the broader predicate and wrongly reset
    495 such rows; migration 0048 restored them.
    """
    if canonical_us_state(hq_state) is not None:
        return False
    if not ((hq_state or "").strip() or (hq_city or "").strip()):
        return False
    stated = (enriched_payload or {}).get("hq_country")
    return not (isinstance(stated, str) and stated.strip())


async def _reset_unevidenced_us(
    session: AsyncSession,
    summary: NormalizeHqStateSummary,
    *,
    limit: int | None,
    dry_run: bool,
) -> None:
    """Pass 2 (see module doc): NULL out US stamps with no evidence behind them.

    Scope: not-excluded rows infer-hq-country never checked (a checked row's
    country carries that stage's verified quote — never second-guessed here).
    ``limit`` bounds the resets, not the scan (the US cohort is a few
    thousand narrow rows).
    """
    rows = (
        await session.execute(
            select(
                Company.id,
                Company.slug,
                Company.hq_state,
                Company.hq_city,
                Company.last_enriched_payload,
            )
            .where(
                Company.hq_country == "US",
                Company.hq_country_checked_at.is_(None),
                Company.exclusion_reason.is_(None),
            )
            .order_by(Company.id)
        )
    ).all()
    summary.us_rows_checked = len(rows)

    for company_id, slug, hq_state, hq_city, payload in rows:
        if limit is not None and summary.unevidenced_us_reset >= limit:
            break
        if not is_unevidenced_us(
            hq_state=hq_state, hq_city=hq_city, enriched_payload=payload
        ):
            continue
        logger.info(
            "normalize-hq-state: unevidenced US reset (slug=%s state=%r city=%r)%s",
            slug,
            hq_state,
            hq_city,
            " [dry-run]" if dry_run else "",
        )
        summary.unevidenced_us_reset += 1
        if dry_run:
            continue
        company = await session.get(Company, company_id)
        if company is None:  # merged away mid-run
            summary.errors += 1
            continue
        company.hq_country = None
        try:
            await session.commit()
        except StaleDataError:
            await session.rollback()
            summary.errors += 1
