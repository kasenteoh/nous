"""normalize-taxonomy stage — recanonicalize companies' free-text taxonomy.

Two parallel free-text taxonomy columns drifted into spelling sprawl (ad-tech /
adtech / advertising technology; biotech / biotech tooling; healthcare /
healthtech / healthcare AI):

  - ``primary_category`` — never normalized in place before this stage, and
  - ``industry_group`` — canonicalized on (re-)enrichment, but only the original
    ~20-bucket map matched, so the long tail kept leaking into the browse
    dropdown until the cron happened to re-enrich a company.

This stage applies the committed string maps — ``util.category.normalize_category``
and ``util.industry.normalize_industry`` — to the existing column values. It is a
pure string op: no LLM, no schema change. Backfilling ``industry_group`` here
(rather than waiting on re-enrichment) heals the whole table in one pass.

It is set-based and idempotent per column: it reads the DISTINCT non-null values
of each column, computes each one's canonical form once, and issues a single
``UPDATE companies SET <col> = <canon> WHERE <col> = <raw>`` for each value that
actually changes. Values already canonical (and unknown values, which pass
through unchanged) are skipped, so a second run finds nothing to update and
writes zero rows.

No migration accompanies this stage — both columns already exist; this only
rewrites their string contents.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from nous.db.models import Company
from nous.util.category import normalize_category
from nous.util.industry import normalize_industry

logger = logging.getLogger(__name__)


class ColumnNormalizeResult(BaseModel):
    """Per-column outcome of one normalize-taxonomy run."""

    distinct_values_seen: int  # distinct non-null values of this column
    values_changed: int  # distinct values whose canonical form differs
    rows_updated: int  # company rows rewritten across all changed values


class NormalizeTaxonomySummary(BaseModel):
    """Result of one normalize-taxonomy run (both taxonomy columns)."""

    primary_category: ColumnNormalizeResult
    industry_group: ColumnNormalizeResult

    # Convenience roll-ups kept for the observability record / back-compat with
    # the pre-industry summary shape (it exposed these three top-level fields).
    distinct_values_seen: int
    values_changed: int
    rows_updated: int


async def _normalize_column(
    session: AsyncSession,
    column: InstrumentedAttribute[str | None],
    label: str,
    normalizer: Callable[[str | None], str | None],
) -> ColumnNormalizeResult:
    """Recanonicalize one free-text taxonomy column in place.

    Reads the column's DISTINCT non-null values, maps each through
    ``normalizer``, and bulk-updates the rows for each value that changes. Does
    NOT commit — the caller commits once after all columns are processed, so the
    whole stage is one transaction.
    """
    # DISTINCT non-null values — bounded (a few hundred at most), so pulling them
    # all is cheap and keeps the per-value UPDATE set-based instead of a per-row
    # Python loop.
    distinct_values = (
        (
            await session.execute(
                select(column).where(column.is_not(None)).distinct()
            )
        )
        .scalars()
        .all()
    )

    values_changed = 0
    rows_updated = 0

    for raw in distinct_values:
        canon = normalizer(raw)
        # The normalizers never return None for a non-null, non-blank input, but
        # a whitespace-only stored value would map to None — leave those
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
                update(Company).where(column == raw).values({column: canon})
            ),
        )
        affected = result.rowcount or 0
        logger.info(
            "normalize-taxonomy[%s]: %r -> %r (%d rows)",
            label,
            raw,
            canon,
            affected,
        )
        values_changed += 1
        rows_updated += affected

    return ColumnNormalizeResult(
        distinct_values_seen=len(distinct_values),
        values_changed=values_changed,
        rows_updated=rows_updated,
    )


async def run_normalize_taxonomy(
    session: AsyncSession,
) -> NormalizeTaxonomySummary:
    """Recanonicalize ``primary_category`` and ``industry_group`` in place.

    For each column, reads its distinct non-null values, maps each through the
    committed normalizer, and bulk-updates the rows for each value that changes.
    Commits once at the end. Idempotent: re-running finds no remaining
    differences (each canonical form is a fixed point of its normalizer).
    """
    category = await _normalize_column(
        session,
        Company.primary_category,
        "primary_category",
        normalize_category,
    )
    industry = await _normalize_column(
        session,
        Company.industry_group,
        "industry_group",
        normalize_industry,
    )

    await session.commit()

    summary = NormalizeTaxonomySummary(
        primary_category=category,
        industry_group=industry,
        distinct_values_seen=(
            category.distinct_values_seen + industry.distinct_values_seen
        ),
        values_changed=category.values_changed + industry.values_changed,
        rows_updated=category.rows_updated + industry.rows_updated,
    )
    logger.info("normalize-taxonomy summary: %s", summary.model_dump_json())
    return summary
