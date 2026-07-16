"""DB-gated tests for migration 0039 + the compute-momentum stage.

Covers, against a real Postgres (schema from ``alembic upgrade head``):

- migration 0039 <-> model: momentum_score/computed_at/why round-trip; an
  unscored company reads NULL score + [] why (the '{}' server_default);
- a rising news_count_30d snapshot series scores > 0.5 with a news "why" chip;
- funding recency alone (no snapshots) still produces a non-NULL score;
- a shown company with neither snapshots nor a funding date scores NULL (low
  confidence — never fabricated), yet is still stamped (evaluated);
- shown-only: an excluded company is never processed (stays unstamped);
- a previously-high score is CLEARED to NULL when the signal disappears (no
  stale high score survives);
- idempotence/determinism: a same-week re-run rewrites byte-identical
  momentum_score (only momentum_computed_at advances); --as-of-week normalizes
  to its Monday;
- the CLI records a pipeline_runs row and persists across sessions.

The isolated ``db`` fixture tests run against a clean CI DB, so exact per-company
assertions hold; aggregate counts use ``>=`` to tolerate any pre-existing rows.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanySnapshot, PipelineRun
from nous.pipeline.compute_momentum import run_compute_momentum

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

AS_OF = date(2026, 7, 13)  # a Monday
NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


def _company(slug: str, **overrides: Any) -> Company:
    defaults: dict[str, Any] = {
        "name": f"Co {slug}",
        "slug": slug,
        "normalized_name": f"co {slug}",
        "description_short": f"Short description for {slug}.",  # -> shown
    }
    defaults.update(overrides)
    return Company(**defaults)


def _snap(
    company_id: object,
    captured_week: date,
    *,
    news: int,
    lo: int | None = None,
    hi: int | None = None,
) -> CompanySnapshot:
    return CompanySnapshot(
        company_id=company_id,
        captured_week=captured_week,
        news_count_30d=news,
        employee_count_min=lo,
        employee_count_max=hi,
    )


async def _reload(db: AsyncSession, slug: str) -> Company:
    return (
        await db.execute(select(Company).where(Company.slug == slug))
    ).scalar_one()


# ---------------------------------------------------------------------------
# Migration 0039 <-> model consistency
# ---------------------------------------------------------------------------


async def test_momentum_columns_round_trip(db: AsyncSession) -> None:
    scored = _company("mom-scored")
    scored.momentum_score = 0.73
    scored.momentum_why = ["news +180%", "raised 3wks ago"]
    scored.momentum_computed_at = NOW
    blank = _company("mom-blank")  # momentum_* left at their defaults
    db.add_all([scored, blank])
    await db.commit()
    db.expire_all()

    got = await _reload(db, "mom-scored")
    assert got.momentum_score == 0.73
    assert got.momentum_why == ["news +180%", "raised 3wks ago"]
    assert got.momentum_computed_at == NOW

    empty = await _reload(db, "mom-blank")
    assert empty.momentum_score is None
    assert empty.momentum_computed_at is None
    # server_default '{}' -> an empty list, never NULL.
    assert empty.momentum_why == []


# ---------------------------------------------------------------------------
# Rising news -> accelerating score
# ---------------------------------------------------------------------------


async def test_rising_news_scores_above_half(db: AsyncSession) -> None:
    co = _company("mom-news")
    db.add(co)
    await db.flush()
    # Recent weeks hot (weeks 0–1), baseline quiet (weeks 3–6).
    db.add_all(
        [
            _snap(co.id, AS_OF, news=15),
            _snap(co.id, AS_OF - timedelta(days=7), news=13),
            _snap(co.id, AS_OF - timedelta(days=14), news=9),  # buffer week
            _snap(co.id, AS_OF - timedelta(days=21), news=3),
            _snap(co.id, AS_OF - timedelta(days=28), news=2),
            _snap(co.id, AS_OF - timedelta(days=35), news=2),
            _snap(co.id, AS_OF - timedelta(days=42), news=1),
        ]
    )
    await db.commit()

    summary = await run_compute_momentum(db, as_of_week=AS_OF, now=NOW)
    assert summary.as_of_week == AS_OF
    assert summary.companies_scored >= 1

    db.expire_all()
    got = await _reload(db, "mom-news")
    assert got.momentum_score is not None and got.momentum_score > 0.5
    assert got.momentum_computed_at == NOW
    assert any("news" in chip for chip in got.momentum_why)


# ---------------------------------------------------------------------------
# Funding-only cold start (no snapshots)
# ---------------------------------------------------------------------------


async def test_funding_only_cold_start_scores(db: AsyncSession) -> None:
    # No description, but funded -> shown via the funding branch; a recent raise
    # gives a real score with zero snapshot history.
    co = _company(
        "mom-funding",
        description_short=None,
        funding_round_count=1,
        latest_round_date=AS_OF - timedelta(days=14),
    )
    db.add(co)
    await db.commit()

    await run_compute_momentum(db, as_of_week=AS_OF, now=NOW)

    db.expire_all()
    got = await _reload(db, "mom-funding")
    assert got.momentum_score is not None and got.momentum_score > 0.5
    assert any("raised" in chip for chip in got.momentum_why)


# ---------------------------------------------------------------------------
# No signal -> NULL (but still evaluated)
# ---------------------------------------------------------------------------


async def test_no_data_is_null_but_stamped(db: AsyncSession) -> None:
    co = _company("mom-null")  # shown (has description), but no snaps / no round
    db.add(co)
    await db.commit()

    summary = await run_compute_momentum(db, as_of_week=AS_OF, now=NOW)
    assert summary.companies_null_low_confidence >= 1

    db.expire_all()
    got = await _reload(db, "mom-null")
    assert got.momentum_score is None
    assert got.momentum_why == []
    # Evaluated this run even though the score is NULL — the stale-clear marker.
    assert got.momentum_computed_at == NOW


# ---------------------------------------------------------------------------
# Shown-only: excluded companies are never processed
# ---------------------------------------------------------------------------


async def test_excluded_company_is_skipped(db: AsyncSession) -> None:
    co = _company(
        "mom-excluded",
        exclusion_reason="not_a_startup",
        funding_round_count=1,
        latest_round_date=AS_OF - timedelta(days=7),
    )
    db.add(co)
    await db.commit()

    await run_compute_momentum(db, as_of_week=AS_OF, now=NOW)

    db.expire_all()
    got = await _reload(db, "mom-excluded")
    # Never a member -> never scored, never even stamped.
    assert got.momentum_score is None
    assert got.momentum_computed_at is None


async def test_exited_cohort_momentum_is_cleared(db: AsyncSession) -> None:
    """A company scored while shown that has since EXITED the cohort (here:
    became excluded) has its momentum columns cleared back to NULL — only
    currently-shown companies carry a score (mirrors compute-completeness's
    exit-cohort clear)."""
    co = _company(
        "mom-exited",
        exclusion_reason="not_a_startup",
        funding_round_count=1,
        latest_round_date=AS_OF - timedelta(days=7),
    )
    co.momentum_score = 0.9  # stale from when it was shown
    co.momentum_why = ["funding recency"]
    co.momentum_computed_at = NOW - timedelta(days=7)
    db.add(co)
    await db.commit()

    summary = await run_compute_momentum(db, as_of_week=AS_OF, now=NOW)

    db.expire_all()
    got = await _reload(db, "mom-exited")
    assert got.momentum_score is None
    assert got.momentum_why is None
    assert got.momentum_computed_at is None
    assert summary.companies_cleared >= 1


# ---------------------------------------------------------------------------
# Stale high score is cleared when the signal disappears
# ---------------------------------------------------------------------------


async def test_stale_high_score_is_cleared_to_null(db: AsyncSession) -> None:
    co = _company("mom-stale")
    co.momentum_score = 0.95  # a stale high score from a prior, richer run
    co.momentum_why = ["news +300%"]
    co.momentum_computed_at = NOW - timedelta(days=7)
    db.add(co)
    await db.commit()

    # Now it has no snapshots and no funding date -> recompute must NULL it.
    await run_compute_momentum(db, as_of_week=AS_OF, now=NOW)

    db.expire_all()
    got = await _reload(db, "mom-stale")
    assert got.momentum_score is None
    assert got.momentum_why == []
    assert got.momentum_computed_at == NOW


# ---------------------------------------------------------------------------
# Idempotence / determinism
# ---------------------------------------------------------------------------


async def test_same_week_rerun_is_byte_identical(db: AsyncSession) -> None:
    co = _company("mom-idem")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            _snap(co.id, AS_OF, news=10, lo=40, hi=60),
            _snap(co.id, AS_OF - timedelta(days=7), news=9),
            _snap(co.id, AS_OF - timedelta(days=28), news=3),
            _snap(co.id, AS_OF - timedelta(days=63), news=2, lo=20, hi=30),
        ]
    )
    co.latest_round_date = AS_OF - timedelta(days=40)
    db.add(co)
    await db.commit()

    await run_compute_momentum(db, as_of_week=AS_OF, now=NOW)
    db.expire_all()
    first = await _reload(db, "mom-idem")
    first_score = first.momentum_score
    first_why = list(first.momentum_why or [])
    assert first_score is not None

    later = NOW + timedelta(days=1)
    await run_compute_momentum(db, as_of_week=AS_OF, now=later)
    db.expire_all()
    second = await _reload(db, "mom-idem")
    # Byte-identical score + why from the same as-of week; only the stamp moves.
    assert second.momentum_score == first_score
    assert list(second.momentum_why or []) == first_why
    assert second.momentum_computed_at == later


async def test_as_of_week_normalizes_to_monday(db: AsyncSession) -> None:
    co = _company("mom-monday", latest_round_date=AS_OF - timedelta(days=10))
    db.add(co)
    await db.commit()

    # Pass a Friday; the stage anchors to that ISO week's Monday.
    friday = AS_OF + timedelta(days=4)
    summary = await run_compute_momentum(db, as_of_week=friday, now=NOW)
    assert summary.as_of_week == AS_OF

    db.expire_all()
    got = await _reload(db, "mom-monday")
    assert got.momentum_score is not None


# ---------------------------------------------------------------------------
# Production path: run_compute_momentum COMMITS (persists across sessions) and
# the CLI records a pipeline_runs row.
#
# Runs the CLI's exact body (run_compute_momentum + record_pipeline_run over
# AsyncSessionLocal) in-loop rather than via CliRunner — the CLI wraps its work
# in asyncio.run(), which cannot nest inside this async test's event loop. The
# Click wiring itself (registration/options) is covered by the --help test in
# test_compute_momentum.py.
# ---------------------------------------------------------------------------


async def test_run_commits_and_records_pipeline_run() -> None:
    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run

    slug = f"mom-cli-{uuid.uuid4().hex[:8]}"
    start = datetime.now(UTC)
    try:
        async with AsyncSessionLocal() as session:
            co = _company(slug, latest_round_date=date.today() - timedelta(days=7))
            session.add(co)
            await session.commit()

        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await run_compute_momentum(session)
        await record_pipeline_run(
            "compute-momentum",
            started_at=started,
            inputs_seen=summary.companies_seen,
            rows_written=summary.companies_scored,
            summary=summary,
        )

        # A FRESH session sees the momentum write -> the stage committed (not
        # just flushed), and the run was recorded.
        async with AsyncSessionLocal() as session:
            scored = (
                await session.execute(select(Company).where(Company.slug == slug))
            ).scalar_one()
            assert scored.momentum_score is not None
            assert scored.momentum_computed_at is not None

            runs = (
                (
                    await session.execute(
                        select(PipelineRun).where(
                            PipelineRun.stage == "compute-momentum",
                            PipelineRun.finished_at >= start,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(runs) >= 1
            assert runs[0].status == "success"
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Company).where(Company.slug == slug))
            await session.execute(
                delete(PipelineRun).where(
                    PipelineRun.stage == "compute-momentum",
                    PipelineRun.finished_at >= start,
                )
            )
            await session.commit()
