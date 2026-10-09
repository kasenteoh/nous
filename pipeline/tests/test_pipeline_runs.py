"""Tests for the pipeline_runs observability recorder.

The pure status-classification tests always run. The persistence/alert test is
DB-gated (it exercises the recorder's own session + commit against the live test
DB, then cleans up its row).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from nous.observability import _run_status, record_pipeline_run

# ---------------------------------------------------------------------------
# _run_status — pure classification (no DB)
# ---------------------------------------------------------------------------


def test_status_success_when_rows_written() -> None:
    assert (
        _run_status(inputs_seen=10, rows_written=5, error=None, flag_empty=True)
        == "success"
    )


def test_status_empty_when_flagged_and_inputs_but_no_output() -> None:
    # The silent-failure signature: processed inputs, wrote nothing.
    assert (
        _run_status(inputs_seen=500, rows_written=0, error=None, flag_empty=True)
        == "empty"
    )


def test_status_success_when_no_inputs() -> None:
    # 0 inputs -> 0 output is not suspicious (nothing eligible this run).
    assert (
        _run_status(inputs_seen=0, rows_written=0, error=None, flag_empty=True)
        == "success"
    )


def test_status_success_when_not_flag_empty() -> None:
    # Stages that legitimately produce 0 (e.g. ingest with no new articles).
    assert (
        _run_status(inputs_seen=400, rows_written=0, error=None, flag_empty=False)
        == "success"
    )


def test_status_error_takes_precedence() -> None:
    assert (
        _run_status(inputs_seen=10, rows_written=5, error="boom", flag_empty=True)
        == "error"
    )


# ---------------------------------------------------------------------------
# record_pipeline_run — persists (commits) + alerts
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration test",
)
async def test_record_persists_committed_and_warns_on_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nous.db.models import PipelineRun
    from nous.db.session import AsyncSessionLocal

    stage = f"test-empty-{uuid.uuid4().hex[:8]}"
    try:
        await record_pipeline_run(
            stage,
            started_at=datetime.now(UTC),
            inputs_seen=42,
            rows_written=0,
            flag_empty=True,
        )

        # Committed: a FRESH session (the recorder used its own) sees the row.
        async with AsyncSessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineRun).where(PipelineRun.stage == stage)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].status == "empty"
        assert rows[0].inputs_seen == 42
        assert rows[0].rows_written == 0

        # Emitted a GitHub Actions warning annotation for the silent-empty run.
        out = capsys.readouterr().out
        assert "::warning::" in out
        assert stage in out
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PipelineRun).where(PipelineRun.stage == stage))
            await session.commit()


# ---------------------------------------------------------------------------
# guard_stage / _run_stage — a CRASHING stage must leave a status='error' row
# (the only signal pipeline-health --strict-errors and the #227 issue alert
# gate on). Recorder is faked: these are pure unit tests.
# ---------------------------------------------------------------------------


class _RecordSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, stage: str, **kwargs: object) -> None:
        self.calls.append({"stage": stage, **kwargs})


@pytest.fixture
def record_spy(monkeypatch: pytest.MonkeyPatch) -> _RecordSpy:
    import nous.observability as observability

    spy = _RecordSpy()
    monkeypatch.setattr(observability, "record_pipeline_run", spy)
    return spy


async def _boom() -> None:
    raise RuntimeError("connection reset by peer")


async def test_guard_stage_records_error_and_reraises(record_spy: _RecordSpy) -> None:
    from nous.observability import guard_stage

    with pytest.raises(RuntimeError, match="connection reset"):
        await guard_stage("scrape-homepages", _boom())

    assert len(record_spy.calls) == 1
    call = record_spy.calls[0]
    assert call["stage"] == "scrape-homepages"
    assert call["error"] == "RuntimeError: connection reset by peer"
    assert call["inputs_seen"] == 0
    assert call["rows_written"] == 0
    # error= is what makes _run_status classify the row as 'error'.
    assert (
        _run_status(inputs_seen=0, rows_written=0, error=str(call["error"]), flag_empty=False)
        == "error"
    )


async def test_guard_stage_success_records_nothing(record_spy: _RecordSpy) -> None:
    from nous.observability import guard_stage

    async def _ok() -> int:
        return 7

    assert await guard_stage("ingest-news", _ok()) == 7
    assert record_spy.calls == []


async def test_guard_stage_ignored_exception_records_nothing(
    record_spy: _RecordSpy,
) -> None:
    from nous.observability import guard_stage

    async def _usage() -> None:
        raise ValueError("bad --firm")

    with pytest.raises(ValueError):
        await guard_stage("refresh-vc-portfolios", _usage(), ignore=(ValueError,))
    assert record_spy.calls == []


def test_format_stage_error_truncates() -> None:
    from nous.observability import _MAX_ERROR_CHARS, format_stage_error

    text = format_stage_error(RuntimeError("x" * 10_000))
    assert len(text) == _MAX_ERROR_CHARS
    assert text.startswith("RuntimeError: xxx")
    assert text.endswith("…")


def test_run_stage_records_crash_under_stage_name(record_spy: _RecordSpy) -> None:
    from nous.cli import _run_stage

    with pytest.raises(RuntimeError):
        _run_stage("resolve-homepages", _boom())
    assert [c["stage"] for c in record_spy.calls] == ["resolve-homepages"]


def test_run_stage_none_skips_recording(record_spy: _RecordSpy) -> None:
    # stage=None is the --dry-run path: its success records no row, so an error
    # row would never be superseded and would alert forever.
    from nous.cli import _run_stage

    with pytest.raises(RuntimeError):
        _run_stage(None, _boom())
    assert record_spy.calls == []


def test_run_stage_click_exception_is_not_a_stage_failure(
    record_spy: _RecordSpy,
) -> None:
    import click

    from nous.cli import _run_stage

    async def _operator_error() -> None:
        raise click.ClickException("no company with slug 'nope'")

    with pytest.raises(click.ClickException):
        _run_stage("verify-sources", _operator_error())
    assert record_spy.calls == []


def test_sum_counts_ignores_bools() -> None:
    from nous.cli import _sum_counts
    from nous.pipeline.repair_catalog import RepairSummary

    summary = RepairSummary(names_cleaned=2, parked_reset=3, dry_run=True)
    assert _sum_counts(summary) == 5


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration test",
)
async def test_crash_then_health_reports_error_until_success() -> None:
    """End to end against the DB: a crash leaves an 'error' row that
    pipeline-health flags; the next successful run supersedes it."""
    from nous.db.models import PipelineRun
    from nous.db.session import AsyncSessionLocal
    from nous.observability import guard_stage
    from nous.pipeline.pipeline_health import run_pipeline_health

    stage = f"test-crash-{uuid.uuid4().hex[:8]}"
    try:
        with pytest.raises(RuntimeError):
            await guard_stage(stage, _boom())

        async with AsyncSessionLocal() as session:
            report = await run_pipeline_health(session)
        mine = [s for s in report.stages if s.stage == stage]
        assert [s.status for s in mine] == ["error"]

        await record_pipeline_run(
            stage, started_at=datetime.now(UTC), inputs_seen=1, rows_written=1
        )
        async with AsyncSessionLocal() as session:
            report = await run_pipeline_health(session)
        mine = [s for s in report.stages if s.stage == stage]
        assert [s.status for s in mine] == ["success"]
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PipelineRun).where(PipelineRun.stage == stage))
            await session.commit()
