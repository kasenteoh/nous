"""DB-gated tests for the db-stats watchdog stage.

Requires DATABASE_URL to point at a Postgres instance that has had
`alembic upgrade head` applied. Skipped automatically when DATABASE_URL
is unset (CI without DB, or unit-test-only runs).

Unit tests that do NOT require a DB live in tests/test_observability.py so
they run in every environment.
"""

from __future__ import annotations

import os

import pytest

from nous.db.base import Base
from nous.pipeline.db_stats import run_db_stats

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB-gated db-stats tests",
)


async def test_every_model_table_appears_with_bytes_gt_zero(
    db: object,
) -> None:
    """Every table in Base.metadata must appear in the summary with bytes > 0."""
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(db, AsyncSession)
    summary = await run_db_stats(db, cap_mb=500, warn_pct=80)

    returned_names = {t.name for t in summary.tables}
    expected_names = set(Base.metadata.tables.keys())

    assert expected_names.issubset(returned_names), (
        f"Missing tables in db-stats output: {expected_names - returned_names}"
    )
    for table in summary.tables:
        assert table.bytes > 0, f"Expected bytes > 0 for table '{table.name}'"


async def test_warn_flips_with_tiny_cap(db: object) -> None:
    """Setting cap_mb=1 forces warn=True; a normal 500 MB cap leaves warn=False."""
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(db, AsyncSession)

    small_cap = await run_db_stats(db, cap_mb=1, warn_pct=80)
    assert small_cap.warn is True, "Expected warn=True with cap_mb=1"

    # The local test DB is far below 500 MB.
    normal_cap = await run_db_stats(db, cap_mb=500, warn_pct=80)
    assert normal_cap.warn is False, "Expected warn=False with cap_mb=500"


async def test_summary_fields_are_consistent(db: object) -> None:
    """pct_of_cap, warn, and cap_bytes must be internally consistent."""
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(db, AsyncSession)
    summary = await run_db_stats(db, cap_mb=500, warn_pct=80)

    assert summary.cap_bytes == 500 * 1024 * 1024
    assert summary.total_bytes > 0
    expected_pct = summary.total_bytes / summary.cap_bytes * 100
    assert abs(summary.pct_of_cap - round(expected_pct, 2)) < 0.01
    assert summary.warn == (summary.pct_of_cap >= 80)


