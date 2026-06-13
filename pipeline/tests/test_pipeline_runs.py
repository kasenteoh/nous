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
