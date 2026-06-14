"""normalize-taxonomy stage — recanonicalize companies.primary_category in place.

`industry_group` is canonicalized on every (re-)enrichment (M1,
`enrich_companies` + `util/industry.py`), but `primary_category` is a second,
parallel free-text taxonomy that was never normalized, so the same spelling
sprawl accumulated there (ad-tech / adtech / advertising technology; biotech /
biotech tooling; dev tools / devtools). This stage applies
`util/category.normalize_category` to the existing column values — a pure string
op, no LLM, no schema change.

It is set-based and idempotent: it reads the DISTINCT non-null
`primary_category` values, computes each one's canonical form once, and issues a
single ``UPDATE companies SET primary_category = <canon> WHERE primary_category
= <raw>`` for each value that actually changes. Values already canonical (and
unknown values, which pass through unchanged) are skipped, so a second run finds
nothing to update and writes zero rows.

No migration accompanies this stage — `primary_category` already exists; this
only rewrites its string contents.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.util.category import normalize_category

logger = logging.getLogger(__name__)


class NormalizeTaxonomySummary(BaseModel):
    """Result of one normalize-taxonomy run."""

    distinct_values_seen: int  # distinct non-null primary_category values
    values_changed: int  # distinct values whose canonical form differs
    rows_updated: int  # company rows rewritten across all changed values


async def run_normalize_taxonomy(
    session: AsyncSession,
) -> NormalizeTaxonomySummary:
    """Recanonicalize every distinct ``primary_category`` value in place.

    Reads the distinct non-null values, maps each through
    ``normalize_category``, and bulk-updates the rows for each value that
    changes. Commits once at the end. Idempotent: re-running finds no remaining
    differences (the canonical form is a fixed point of ``normalize_category``).
    """
    # DISTINCT non-null primary_category values — bounded (a few hundred at
    # most), so pulling them all is cheap and keeps the per-value UPDATE
    # set-based instead of a per-row Python loop.
    distinct_values = (
        (
            await session.execute(
                select(Company.primary_category)
                .where(Company.primary_category.is_not(None))
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    values_changed = 0
    rows_updated = 0

    for raw in distinct_values:
        canon = normalize_category(raw)
        # normalize_category never returns None for a non-null, non-blank input,
        # but a whitespace-only stored value would map to None — leave those
        # untouched rather than writing a NULL over them.
        if canon is None or canon == raw:
            continue

        # An UPDATE returns a CursorResult whose rowcount is the number of rows
        # matched/changed. session.execute is typed as the broader Result, so we
        # narrow explicitly. rowcount is reliable here (a plain UPDATE ... WHERE
        # under psycopg) — unlike the INSERT ... SELECT in snapshot_companies.
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(Company)
                .where(Company.primary_category == raw)
                .values(primary_category=canon)
            ),
        )
        affected = result.rowcount or 0
        logger.info(
            "normalize-taxonomy: %r -> %r (%d rows)", raw, canon, affected
        )
        values_changed += 1
        rows_updated += affected

    await session.commit()

    summary = NormalizeTaxonomySummary(
        distinct_values_seen=len(distinct_values),
        values_changed=values_changed,
        rows_updated=rows_updated,
    )
    logger.info(
        "normalize-taxonomy summary: %s", summary.model_dump_json()
    )
    return summary
