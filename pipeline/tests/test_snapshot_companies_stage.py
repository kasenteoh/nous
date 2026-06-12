"""DB-gated tests for the snapshot-companies stage.

The stage is a single set-based INSERT ... SELECT ... ON CONFLICT DO UPDATE, so
these tests assert behavior, not internals: one row per company per week, the
trailing-30-day news window (article inside vs outside the window), same-week
idempotency (run twice -> stable row count, refreshed values), and the --week
backfill override landing on the chosen week's Monday.

Gated on DATABASE_URL like the other integration tests.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanySnapshot, NewsArticle
from nous.pipeline.snapshot_companies import iso_week_monday, run_snapshot_companies

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(
    *,
    slug: str,
    name: str = "Acme",
    employee_count_min: int | None = None,
    employee_count_max: int | None = None,
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        employee_count_min=employee_count_min,
        employee_count_max=employee_count_max,
    )


def _make_article(
    *, company_id: object, url: str, published_date: date | None
) -> NewsArticle:
    return NewsArticle(
        company_id=company_id,
        url=url,
        title="t",
        source="example.com",
        published_date=published_date,
        raw_content="x",
    )


async def _snapshots_for(
    db: AsyncSession, company_id: object
) -> list[CompanySnapshot]:
    # populate_existing() forces a refresh from the DB so any stale row left in
    # the identity map by a prior run (the session uses expire_on_commit=False)
    # reflects the values the stage just upserted.
    result = await db.execute(
        select(CompanySnapshot)
        .where(CompanySnapshot.company_id == company_id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# iso_week_monday helper
# ---------------------------------------------------------------------------


def test_iso_week_monday_normalizes_to_monday() -> None:
    # 2026-06-12 is a Friday; its ISO-week Monday is 2026-06-08.
    assert iso_week_monday(date(2026, 6, 12)) == date(2026, 6, 8)
    # A Monday maps to itself.
    assert iso_week_monday(date(2026, 6, 8)) == date(2026, 6, 8)
    # A Sunday maps back to that week's Monday.
    assert iso_week_monday(date(2026, 6, 14)) == date(2026, 6, 8)


# ---------------------------------------------------------------------------
# One row per company
# ---------------------------------------------------------------------------


async def test_one_row_per_company(db: AsyncSession) -> None:
    c1 = _make_company(slug="snap-a", employee_count_min=10, employee_count_max=50)
    c2 = _make_company(slug="snap-b")
    db.add_all([c1, c2])
    await db.flush()

    week = date(2026, 6, 8)
    summary = await run_snapshot_companies(db, week=week)
    await db.flush()

    assert summary.captured_week == week
    rows1 = await _snapshots_for(db, c1.id)
    rows2 = await _snapshots_for(db, c2.id)
    assert len(rows1) == 1
    assert len(rows2) == 1

    snap1 = rows1[0]
    assert snap1.captured_week == week
    assert snap1.employee_count_min == 10
    assert snap1.employee_count_max == 50
    # No employee data on c2 -> nulls carried through.
    assert rows2[0].employee_count_min is None
    assert rows2[0].employee_count_max is None


# ---------------------------------------------------------------------------
# 30-day news window correctness
# ---------------------------------------------------------------------------


async def test_news_30_day_window(db: AsyncSession) -> None:
    company = _make_company(slug="snap-news")
    db.add(company)
    await db.flush()

    week = date(2026, 6, 8)
    # Inside the trailing-30-day window (week - 29 days): counts.
    inside = _make_article(
        company_id=company.id,
        url="https://example.com/inside",
        published_date=week - timedelta(days=29),
    )
    # Exactly on the boundary (week - 30 days): inclusive -> counts.
    on_boundary = _make_article(
        company_id=company.id,
        url="https://example.com/boundary",
        published_date=week - timedelta(days=30),
    )
    # Outside the window (week - 31 days): does not count.
    outside = _make_article(
        company_id=company.id,
        url="https://example.com/outside",
        published_date=week - timedelta(days=31),
    )
    # Null published_date: cannot be placed in the window -> does not count.
    undated = _make_article(
        company_id=company.id,
        url="https://example.com/undated",
        published_date=None,
    )
    db.add_all([inside, on_boundary, outside, undated])
    await db.flush()

    await run_snapshot_companies(db, week=week)
    await db.flush()

    rows = await _snapshots_for(db, company.id)
    assert len(rows) == 1
    # inside + on_boundary = 2; outside and undated excluded.
    assert rows[0].news_count_30d == 2


# ---------------------------------------------------------------------------
# Same-week idempotency: run twice -> stable count, refreshed values
# ---------------------------------------------------------------------------


async def test_same_week_idempotent_refreshes_in_place(db: AsyncSession) -> None:
    company = _make_company(
        slug="snap-idem", employee_count_min=5, employee_count_max=10
    )
    db.add(company)
    await db.flush()

    week = date(2026, 6, 8)
    await run_snapshot_companies(db, week=week)
    await db.flush()

    rows = await _snapshots_for(db, company.id)
    assert len(rows) == 1
    assert rows[0].employee_count_max == 10
    first_id = rows[0].id

    # Headcount grows + a fresh in-window article appears before the re-run.
    company.employee_count_min = 20
    company.employee_count_max = 40
    db.add(company)
    db.add(
        _make_article(
            company_id=company.id,
            url="https://example.com/new",
            published_date=week - timedelta(days=1),
        )
    )
    await db.flush()

    await run_snapshot_companies(db, week=week)
    await db.flush()

    rows = await _snapshots_for(db, company.id)
    # Still exactly one row for the week — upsert, not append.
    assert len(rows) == 1
    # Same physical row updated in place.
    assert rows[0].id == first_id
    # Values refreshed.
    assert rows[0].employee_count_min == 20
    assert rows[0].employee_count_max == 40
    assert rows[0].news_count_30d == 1


# ---------------------------------------------------------------------------
# --week backfill override lands on that week's Monday
# ---------------------------------------------------------------------------


async def test_week_override_normalizes_to_monday(db: AsyncSession) -> None:
    company = _make_company(slug="snap-week")
    db.add(company)
    await db.flush()

    # Pass a Friday; the stage must store the ISO-week Monday (2026-06-08).
    friday = date(2026, 6, 12)
    summary = await run_snapshot_companies(db, week=friday)
    await db.flush()

    assert summary.captured_week == date(2026, 6, 8)
    rows = await _snapshots_for(db, company.id)
    assert len(rows) == 1
    assert rows[0].captured_week == date(2026, 6, 8)


async def test_summary_counts_rows(db: AsyncSession) -> None:
    before = (
        await db.execute(select(func.count()).select_from(Company))
    ).scalar_one()
    c1 = _make_company(slug="snap-count-a")
    c2 = _make_company(slug="snap-count-b")
    db.add_all([c1, c2])
    await db.flush()

    summary = await run_snapshot_companies(db, week=date(2026, 6, 8))
    await db.flush()

    # The stage snapshots every company in the table, including any pre-existing
    # rows from other fixtures — assert it covered at least the two we added.
    assert summary.snapshot_count >= 2
    assert summary.snapshot_count == before + 2
