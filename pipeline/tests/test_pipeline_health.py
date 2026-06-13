"""Tests for the pipeline-health check stage.

Pure unit tests (no DB required):
  - HealthReport.all_green / .bad computed correctly
  - emit_health_annotations prints correct annotations for each status
  - write_step_summary integration (via monkeypatched env var)

DB-gated integration tests (requires DATABASE_URL):
  - Seeds real pipeline_runs rows; verifies run_pipeline_health returns them.
  - Confirms per-stage latest-run selection (only the most recent row per stage).
  - Verifies empty / error detection round-trips through the DB.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from nous.pipeline.pipeline_health import (
    HealthReport,
    StageHealth,
    emit_health_annotations,
)

# ---------------------------------------------------------------------------
# Unit tests — no DB
# ---------------------------------------------------------------------------


def _make_stage(stage: str, status: str, rows: int = 5, inputs: int = 10) -> StageHealth:
    return StageHealth(stage=stage, status=status, rows_written=rows, inputs_seen=inputs)


def test_all_green_when_all_success() -> None:
    report = HealthReport(
        stages=[
            _make_stage("ingest-news", "success"),
            _make_stage("enrich-companies", "success"),
        ]
    )
    assert report.all_green is True
    assert report.bad == []


def test_all_green_false_when_empty_present() -> None:
    report = HealthReport(
        stages=[
            _make_stage("ingest-news", "success"),
            _make_stage("enrich-companies", "empty", rows=0),
        ]
    )
    assert report.all_green is False
    assert len(report.bad) == 1
    assert report.bad[0].stage == "enrich-companies"


def test_all_green_false_when_error_present() -> None:
    report = HealthReport(
        stages=[
            _make_stage("extract-funding", "error", rows=0, inputs=0),
        ]
    )
    assert report.all_green is False
    assert report.bad[0].status == "error"


def test_bad_lists_both_empty_and_error() -> None:
    report = HealthReport(
        stages=[
            _make_stage("stage-a", "success"),
            _make_stage("stage-b", "empty", rows=0),
            _make_stage("stage-c", "error", rows=0),
        ]
    )
    assert len(report.bad) == 2
    bad_stages = {s.stage for s in report.bad}
    assert bad_stages == {"stage-b", "stage-c"}


# ---------------------------------------------------------------------------
# emit_health_annotations — annotation format
# ---------------------------------------------------------------------------


def test_emit_annotations_green(capsys: pytest.CaptureFixture[str]) -> None:
    report = HealthReport(
        stages=[_make_stage("ingest-news", "success")]
    )
    emit_health_annotations(report)
    out = capsys.readouterr().out
    assert "all stages green" in out
    assert "::warning::" not in out
    assert "::error::" not in out


def test_emit_annotations_empty_stage(capsys: pytest.CaptureFixture[str]) -> None:
    report = HealthReport(
        stages=[_make_stage("enrich-companies", "empty", rows=0, inputs=50)]
    )
    emit_health_annotations(report)
    out = capsys.readouterr().out
    # Must emit a warning annotation, not an error annotation
    assert "::warning::" in out
    assert "enrich-companies" in out
    assert "empty" in out
    assert "::error::" not in out


def test_emit_annotations_error_stage(capsys: pytest.CaptureFixture[str]) -> None:
    report = HealthReport(
        stages=[_make_stage("analyze-competitors", "error", rows=0, inputs=0)]
    )
    emit_health_annotations(report)
    out = capsys.readouterr().out
    # Must emit an error annotation
    assert "::error::" in out
    assert "analyze-competitors" in out
    assert "error" in out


def test_emit_annotations_mixed(capsys: pytest.CaptureFixture[str]) -> None:
    """Both ::warning:: and ::error:: present for mixed bad stages."""
    report = HealthReport(
        stages=[
            _make_stage("ingest-news", "success"),
            _make_stage("enrich-companies", "empty", rows=0, inputs=10),
            _make_stage("extract-funding", "error", rows=0, inputs=0),
        ]
    )
    emit_health_annotations(report)
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "::error::" in out


def test_step_summary_written_on_bad_stages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """emit_health_annotations writes markdown when GITHUB_STEP_SUMMARY is set."""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    report = HealthReport(
        stages=[_make_stage("enrich-companies", "empty", rows=0, inputs=5)]
    )
    emit_health_annotations(report)

    content = summary_file.read_text()
    assert "enrich-companies" in content
    assert "empty" in content


def test_step_summary_written_on_green(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Green runs still write a summary table (for visibility)."""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    report = HealthReport(stages=[_make_stage("ingest-news", "success")])
    emit_health_annotations(report)

    content = summary_file.read_text()
    assert "ingest-news" in content
    assert "success" in content


# ---------------------------------------------------------------------------
# DB-gated integration tests
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark_db = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB-gated pipeline-health tests",
)


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB-gated pipeline-health tests",
)
async def test_run_pipeline_health_detects_empty_stage(
    db: object,
) -> None:
    """Seeds an 'empty' run; run_pipeline_health must flag it in .bad."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from nous.db.models import PipelineRun
    from nous.pipeline.pipeline_health import run_pipeline_health

    assert isinstance(db, AsyncSession)

    stage = f"test-health-empty-{uuid.uuid4().hex[:8]}"
    db.add(
        PipelineRun(
            stage=stage,
            started_at=_now(),
            finished_at=_now(),
            status="empty",
            inputs_seen=20,
            rows_written=0,
        )
    )
    await db.flush()

    report = await run_pipeline_health(db)

    matching = [s for s in report.bad if s.stage == stage]
    assert len(matching) == 1, f"Expected 1 bad entry for {stage}, got {report.bad}"
    assert matching[0].status == "empty"
    assert matching[0].inputs_seen == 20
    assert matching[0].rows_written == 0


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB-gated pipeline-health tests",
)
async def test_run_pipeline_health_detects_error_stage(
    db: object,
) -> None:
    """Seeds an 'error' run; run_pipeline_health must flag it in .bad."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from nous.db.models import PipelineRun
    from nous.pipeline.pipeline_health import run_pipeline_health

    assert isinstance(db, AsyncSession)

    stage = f"test-health-error-{uuid.uuid4().hex[:8]}"
    db.add(
        PipelineRun(
            stage=stage,
            started_at=_now(),
            finished_at=_now(),
            status="error",
            inputs_seen=0,
            rows_written=0,
            error="Something exploded",
        )
    )
    await db.flush()

    report = await run_pipeline_health(db)

    matching = [s for s in report.bad if s.stage == stage]
    assert len(matching) == 1
    assert matching[0].status == "error"


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB-gated pipeline-health tests",
)
async def test_run_pipeline_health_success_stage_not_in_bad(
    db: object,
) -> None:
    """A 'success' stage must NOT appear in report.bad."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from nous.db.models import PipelineRun
    from nous.pipeline.pipeline_health import run_pipeline_health

    assert isinstance(db, AsyncSession)

    stage = f"test-health-ok-{uuid.uuid4().hex[:8]}"
    db.add(
        PipelineRun(
            stage=stage,
            started_at=_now(),
            finished_at=_now(),
            status="success",
            inputs_seen=10,
            rows_written=8,
        )
    )
    await db.flush()

    report = await run_pipeline_health(db)

    bad_stages = {s.stage for s in report.bad}
    assert stage not in bad_stages, (
        f"Success stage {stage!r} should not appear in bad: {bad_stages}"
    )


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB-gated pipeline-health tests",
)
async def test_run_pipeline_health_uses_latest_run_per_stage(
    db: object,
) -> None:
    """Only the LATEST row per stage is checked.

    Seed two rows for the same stage: an earlier 'empty' then a newer 'success'.
    The health check must see 'success' (the most recent), not 'empty'.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from nous.db.models import PipelineRun
    from nous.pipeline.pipeline_health import run_pipeline_health

    assert isinstance(db, AsyncSession)

    stage = f"test-health-latest-{uuid.uuid4().hex[:8]}"
    older_time = _now() - timedelta(hours=3)
    newer_time = _now()

    db.add(
        PipelineRun(
            stage=stage,
            started_at=older_time,
            finished_at=older_time,
            status="empty",
            inputs_seen=5,
            rows_written=0,
        )
    )
    db.add(
        PipelineRun(
            stage=stage,
            started_at=newer_time,
            finished_at=newer_time,
            status="success",
            inputs_seen=5,
            rows_written=5,
        )
    )
    await db.flush()

    report = await run_pipeline_health(db)

    # The stage must appear exactly once — the latest run (success).
    stage_entries = [s for s in report.stages if s.stage == stage]
    assert len(stage_entries) == 1, (
        f"Expected exactly one entry for {stage}, got {stage_entries}"
    )
    assert stage_entries[0].status == "success", (
        f"Latest run should be 'success', got '{stage_entries[0].status}'"
    )
    bad_stages = {s.stage for s in report.bad}
    assert stage not in bad_stages


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB-gated pipeline-health tests",
)
async def test_run_pipeline_health_empty_table_returns_empty_report(
    db: object,
) -> None:
    """When pipeline_runs has no rows for our prefix, report.stages may be empty
    or contain only other stages.  The key property: all_green is True if all
    returned stages are success.

    This test verifies that run_pipeline_health never raises on an empty table.
    We can't guarantee a truly empty table in the shared test DB, so we just
    assert it returns a HealthReport (not raises).
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from nous.pipeline.pipeline_health import run_pipeline_health

    assert isinstance(db, AsyncSession)

    # Guaranteed to not raise even on a completely empty table.
    report = await run_pipeline_health(db)
    assert isinstance(report, HealthReport)
