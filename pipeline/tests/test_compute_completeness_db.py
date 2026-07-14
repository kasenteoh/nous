"""DB-gated tests for migration 0042 + the compute-completeness stage.

Covers, against a real Postgres (schema from ``alembic upgrade head``):

- migration 0042 <-> model: completeness_score/computed_at round-trip; an
  unscored company reads NULL score + NULL stamp;
- a fully-complete company (every scored field + a person) scores 1.0;
- a description-only shown company scores 0.20 (website/description weights);
- has_people wiring: adding a Person lifts the score by its 0.10 weight;
- a funding-only company (no description) is shown via funding and scores 0.15;
- shown-only: an excluded company is never processed (stays unscored/unstamped);
- exited-cohort clearing: a previously-scored company that loses both description
  and funding is reset to NULL (no stale "documented" badge can render);
- ``--limit`` caps the run to the first N shown companies (and skips clearing);
- idempotence/determinism: a same-DB-state re-run rewrites byte-identical
  completeness_score (only completeness_computed_at advances);
- the CLI records a pipeline_runs row and persists across sessions.

The isolated ``db`` fixture tests run against a clean CI DB, so exact per-company
assertions hold; aggregate counts use ``>=`` to tolerate any pre-existing rows.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Person, PipelineRun
from nous.pipeline.compute_completeness import run_compute_completeness

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def _company(slug: str, **overrides: Any) -> Company:
    defaults: dict[str, Any] = {
        "name": f"Co {slug}",
        "slug": slug,
        "normalized_name": f"co {slug}",
        "description_short": f"Short description for {slug}.",  # -> shown
    }
    defaults.update(overrides)
    return Company(**defaults)


async def _reload(db: AsyncSession, slug: str) -> Company:
    return (
        await db.execute(select(Company).where(Company.slug == slug))
    ).scalar_one()


# ---------------------------------------------------------------------------
# Migration 0042 <-> model consistency
# ---------------------------------------------------------------------------


async def test_completeness_columns_round_trip(db: AsyncSession) -> None:
    scored = _company("comp-scored")
    scored.completeness_score = 0.73
    scored.completeness_computed_at = NOW
    blank = _company("comp-blank")  # completeness_* left at their defaults
    db.add_all([scored, blank])
    await db.commit()
    db.expire_all()

    got = await _reload(db, "comp-scored")
    assert got.completeness_score == 0.73
    assert got.completeness_computed_at == NOW

    empty = await _reload(db, "comp-blank")
    assert empty.completeness_score is None
    assert empty.completeness_computed_at is None


# ---------------------------------------------------------------------------
# Fully complete -> 1.0
# ---------------------------------------------------------------------------


async def test_fully_complete_company_scores_one(db: AsyncSession) -> None:
    co = _company(
        "comp-full",
        website="https://full.example/",
        funding_round_count=2,
        hq_country="US",
        industry_group="AI",
        logo_url="https://full.example/logo.png",
        tags=["ai"],
        employee_count_min=10,
        employee_count_max=50,
    )
    db.add(co)
    await db.flush()
    db.add(Person(company_id=co.id, name="Ada", role="CEO", rank=1))
    await db.commit()

    summary = await run_compute_completeness(db, now=NOW)
    assert summary.companies_seen >= 1
    assert summary.companies_scored >= 1

    db.expire_all()
    got = await _reload(db, "comp-full")
    assert got.completeness_score == 1.0
    assert got.completeness_computed_at == NOW


# ---------------------------------------------------------------------------
# Description-only shown company -> 0.20
# ---------------------------------------------------------------------------


async def test_description_only_scores_weight(db: AsyncSession) -> None:
    co = _company("comp-desc")  # description_short only (default) -> shown
    db.add(co)
    await db.commit()

    await run_compute_completeness(db, now=NOW)

    db.expire_all()
    got = await _reload(db, "comp-desc")
    assert got.completeness_score == 0.20  # has_description weight only
    assert got.completeness_computed_at == NOW


# ---------------------------------------------------------------------------
# has_people wiring: a Person lifts the score by its 0.10 weight
# ---------------------------------------------------------------------------


async def test_people_membership_lifts_score(db: AsyncSession) -> None:
    without = _company("comp-nopeople", website="https://np.example/")  # 0.40
    withp = _company("comp-people", website="https://p.example/")  # 0.40 + people
    db.add_all([without, withp])
    await db.flush()
    db.add(Person(company_id=withp.id, name="Grace", role="CTO", rank=1))
    await db.commit()

    await run_compute_completeness(db, now=NOW)

    db.expire_all()
    assert (await _reload(db, "comp-nopeople")).completeness_score == 0.40
    # website(0.20) + description(0.20) + people(0.10)
    assert (await _reload(db, "comp-people")).completeness_score == 0.50


# ---------------------------------------------------------------------------
# Funding-only cold start: shown via funding, no description -> 0.15
# ---------------------------------------------------------------------------


async def test_funding_only_is_shown_and_scored(db: AsyncSession) -> None:
    co = _company("comp-funding", description_short=None, funding_round_count=1)
    db.add(co)
    await db.commit()

    await run_compute_completeness(db, now=NOW)

    db.expire_all()
    got = await _reload(db, "comp-funding")
    assert got.completeness_score == 0.15  # has_funding weight only
    assert got.completeness_computed_at == NOW


# ---------------------------------------------------------------------------
# Shown-only: excluded companies are never processed
# ---------------------------------------------------------------------------


async def test_excluded_company_is_skipped(db: AsyncSession) -> None:
    co = _company(
        "comp-excluded",
        exclusion_reason="not_a_startup",
        website="https://ex.example/",
    )
    db.add(co)
    await db.commit()

    await run_compute_completeness(db, now=NOW)

    db.expire_all()
    got = await _reload(db, "comp-excluded")
    # Never a member -> never scored, never even stamped.
    assert got.completeness_score is None
    assert got.completeness_computed_at is None


# ---------------------------------------------------------------------------
# Exited-cohort clearing: a scored company that loses both signals -> NULL
# ---------------------------------------------------------------------------


async def test_exited_cohort_score_is_cleared(db: AsyncSession) -> None:
    # Was scored 0.70 in a prior, richer run; then stripped to a husk (no
    # description, no funding) — e.g. repair_catalog parking a dead domain — so it
    # exits the shown cohort. The stage must reset it to NULL so no stale
    # "documented" badge survives.
    co = _company("comp-exit", website="https://exit.example/")
    co.completeness_score = 0.70
    co.completeness_computed_at = NOW - timedelta(days=7)
    db.add(co)
    await db.flush()
    co.description_short = None  # website alone does NOT make it shown
    co.funding_round_count = 0
    db.add(co)
    await db.commit()

    summary = await run_compute_completeness(db, now=NOW)
    assert summary.companies_cleared >= 1

    db.expire_all()
    got = await _reload(db, "comp-exit")
    assert got.completeness_score is None
    assert got.completeness_computed_at is None


async def test_shown_company_is_not_cleared(db: AsyncSession) -> None:
    # A currently-shown scored company must never be swept by the clear-stale pass.
    co = _company("comp-kept", website="https://kept.example/")  # shown (desc)
    db.add(co)
    await db.commit()

    await run_compute_completeness(db, now=NOW)
    db.expire_all()
    got = await _reload(db, "comp-kept")
    assert got.completeness_score == 0.40  # website + description
    assert got.completeness_computed_at == NOW


# ---------------------------------------------------------------------------
# --limit caps the run (and skips the global clear-stale pass)
# ---------------------------------------------------------------------------


async def test_limit_caps_the_run_and_skips_clearing(db: AsyncSession) -> None:
    db.add_all([_company("comp-lim-a"), _company("comp-lim-b")])
    # A previously-scored, now-exited company that a FULL run would clear.
    exited = _company("comp-lim-exited", description_short=None, funding_round_count=0)
    exited.completeness_score = 0.55
    exited.completeness_computed_at = NOW - timedelta(days=7)
    db.add(exited)
    await db.commit()

    summary = await run_compute_completeness(db, limit=1, now=NOW)
    # Exactly one company processed regardless of how many are shown.
    assert summary.companies_seen == 1
    assert summary.companies_scored == 1
    # A bounded run does NOT do global cleanup: the exited company keeps its score.
    assert summary.companies_cleared == 0
    db.expire_all()
    assert (await _reload(db, "comp-lim-exited")).completeness_score == 0.55


# ---------------------------------------------------------------------------
# Idempotence / determinism
# ---------------------------------------------------------------------------


async def test_same_state_rerun_is_byte_identical(db: AsyncSession) -> None:
    co = _company(
        "comp-idem",
        website="https://idem.example/",
        funding_round_count=1,
        industry_group="AI",
    )
    db.add(co)
    await db.commit()

    await run_compute_completeness(db, now=NOW)
    db.expire_all()
    first = await _reload(db, "comp-idem")
    first_score = first.completeness_score
    assert first_score is not None

    later = NOW + timedelta(days=7)
    await run_compute_completeness(db, now=later)
    db.expire_all()
    second = await _reload(db, "comp-idem")
    # Byte-identical score from the same DB state; only the stamp moves.
    assert second.completeness_score == first_score
    assert second.completeness_computed_at == later


# ---------------------------------------------------------------------------
# Production path: run_compute_completeness COMMITS (persists across sessions)
# and the CLI records a pipeline_runs row.
#
# Runs the CLI's exact body (run_compute_completeness + record_pipeline_run over
# AsyncSessionLocal) in-loop rather than via CliRunner — the CLI wraps its work in
# asyncio.run(), which cannot nest inside this async test's event loop.
# ---------------------------------------------------------------------------


async def test_run_commits_and_records_pipeline_run() -> None:
    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run

    slug = f"comp-cli-{uuid.uuid4().hex[:8]}"
    start = datetime.now(UTC)
    try:
        async with AsyncSessionLocal() as session:
            co = _company(slug, website="https://cli.example/")
            session.add(co)
            await session.commit()

        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await run_compute_completeness(session)
        await record_pipeline_run(
            "compute-completeness",
            started_at=started,
            inputs_seen=summary.companies_seen,
            rows_written=summary.companies_scored,
            summary=summary,
        )

        # A FRESH session sees the completeness write -> the stage committed (not
        # just flushed), and the run was recorded.
        async with AsyncSessionLocal() as session:
            scored = (
                await session.execute(select(Company).where(Company.slug == slug))
            ).scalar_one()
            assert scored.completeness_score is not None
            assert scored.completeness_computed_at is not None

            runs = (
                (
                    await session.execute(
                        select(PipelineRun).where(
                            PipelineRun.stage == "compute-completeness",
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
                    PipelineRun.stage == "compute-completeness",
                    PipelineRun.finished_at >= start,
                )
            )
            await session.commit()
