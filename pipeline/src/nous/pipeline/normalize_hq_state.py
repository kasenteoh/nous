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

    logger.info(
        "normalize-hq-state: seen=%d normalized=%d errors=%d",
        summary.companies_seen,
        summary.normalized,
        summary.errors,
    )
    return summary
