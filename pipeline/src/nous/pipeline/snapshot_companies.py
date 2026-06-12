"""Weekly company-snapshot stage — start of the time-series moat.

Captures one row per company per ISO week into ``company_snapshots``: the
headcount range (mirrored from ``companies``) and the trailing-30-day news
volume as they stand at capture time. Wave-4 momentum charts read this table;
it costs nothing to accumulate now and cannot be reconstructed retroactively,
so we record early.

The whole stage is ONE set-based statement — an
``INSERT INTO company_snapshots (...) SELECT ... FROM companies ...`` whose
``news_count_30d`` column is a correlated scalar subquery over
``news_articles`` — followed by ``ON CONFLICT (company_id, captured_week) DO
UPDATE``. No per-company Python loop: it is a single round-trip, cheap on the
free tier, and the UNIQUE + upsert make a same-week re-run refresh values in
place rather than append (idempotent).

``captured_week`` defaults to the ISO-week Monday of today; ``--week`` supports
backfill and is normalized to that week's Monday.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from pydantic import BaseModel
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanySnapshot, NewsArticle

logger = logging.getLogger(__name__)

# Trailing news window, in days, counted back from captured_week (inclusive on
# both ends: an article published exactly NEWS_WINDOW_DAYS before the week
# Monday still counts).
NEWS_WINDOW_DAYS = 30


class SnapshotSummary(BaseModel):
    """Result of one snapshot-companies run."""

    captured_week: date
    snapshot_count: int  # rows inserted-or-updated this run


def iso_week_monday(day: date) -> date:
    """Return the Monday of the ISO week containing ``day``.

    ``date.weekday()`` is 0 for Monday, so subtracting it lands on Monday.
    Idempotent: a Monday maps to itself.
    """
    return day - timedelta(days=day.weekday())


async def run_snapshot_companies(
    session: AsyncSession,
    *,
    week: date | None = None,
) -> SnapshotSummary:
    """Snapshot every company's momentum signals for one ISO week.

    Builds a single ``INSERT ... SELECT ... ON CONFLICT DO UPDATE`` so the run
    is one statement, cheap, and idempotent: re-running for the same week
    upserts the same rows (UNIQUE (company_id, captured_week)) rather than
    appending, refreshing the headcount range and 30-day news count in place.

    ``week`` defaults to the ISO-week Monday of today; any date passed is
    normalized to its week's Monday so a ``--week`` backfill always lands on a
    Monday.
    """
    captured_week = iso_week_monday(week if week is not None else date.today())
    window_start = captured_week - timedelta(days=NEWS_WINDOW_DAYS)

    # Correlated scalar subquery: trailing-30-day published news count for the
    # outer company. published_date NULL rows fall out of the BETWEEN, matching
    # "cannot be placed in the window -> does not count". COALESCE-free: COUNT
    # never returns NULL, so the column is always a concrete integer (the table
    # column is NOT NULL).
    news_count = (
        select(func.count())
        .select_from(NewsArticle)
        .where(
            NewsArticle.company_id == Company.id,
            NewsArticle.published_date >= window_start,
            NewsArticle.published_date <= captured_week,
        )
        .correlate(Company)
        .scalar_subquery()
    )

    # The SELECT feeding the INSERT. Column order here must match the
    # from_select() column list below exactly.
    snapshot_select = select(
        # id — gen_random_uuid() is a core builtin since PG 13 (Supabase is PG 15)
        func.gen_random_uuid(),
        Company.id,
        literal(captured_week),
        Company.employee_count_min,
        Company.employee_count_max,
        news_count,
    )

    base_insert = pg_insert(CompanySnapshot).from_select(
        [
            "id",
            "company_id",
            "captured_week",
            "employee_count_min",
            "employee_count_max",
            "news_count_30d",
        ],
        snapshot_select,
    )
    # On a same-week conflict, overwrite the stored signals with this run's
    # freshly-computed values (the EXCLUDED row), so the snapshot reflects the
    # latest headcount + news count rather than the stale first capture. The
    # row's identity (id, company_id, captured_week, created_at) is preserved.
    insert_stmt = base_insert.on_conflict_do_update(
        constraint="uq_company_snapshots_company_week",
        set_={
            "employee_count_min": base_insert.excluded.employee_count_min,
            "employee_count_max": base_insert.excluded.employee_count_max,
            "news_count_30d": base_insert.excluded.news_count_30d,
            "updated_at": func.now(),
        },
    )

    await session.execute(insert_stmt)
    await session.commit()

    # rowcount is unreliable for INSERT ... FROM SELECT under asyncpg (returns
    # -1), so report the authoritative count: snapshot rows now present for this
    # week. After the upsert this equals the number of companies snapshotted.
    count_result = await session.execute(
        select(func.count())
        .select_from(CompanySnapshot)
        .where(CompanySnapshot.captured_week == captured_week)
    )
    snapshot_count = count_result.scalar_one()

    logger.info(
        "snapshot-companies: captured_week=%s rows=%d",
        captured_week.isoformat(),
        snapshot_count,
    )
    return SnapshotSummary(captured_week=captured_week, snapshot_count=snapshot_count)
