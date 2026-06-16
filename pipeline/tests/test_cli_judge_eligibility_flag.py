"""CLI plumbing test for ``judge-eligibility --rejudge-nonstartup-signals``.

The stage BEHAVIOR (which rows the flag re-judges, the restamp, idempotency) is
covered by test_judge_eligibility.py. This test covers only the wiring: that the
Click flag exists, defaults to False, and is forwarded verbatim to
``run_judge_eligibility``. No DATABASE_URL needed — the stage call, the session
factory, and the telemetry emit are all stubbed, so the command's coroutine runs
without touching Postgres.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from nous.cli import cli
from nous.pipeline.judge_eligibility import JudgeEligibilitySummary


@pytest.fixture()
def _stub_stage(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub the stage + its side-effecting helpers so the command runs DB-free.

    Returns the AsyncMock standing in for ``run_judge_eligibility`` so a test can
    inspect the kwargs it was called with.
    """
    stage = AsyncMock(return_value=JudgeEligibilitySummary())
    # Patch at the SOURCE modules — the command imports these names locally
    # inside its coroutine, so the lookups resolve against the live modules.
    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.run_judge_eligibility", stage
    )
    monkeypatch.setattr(
        "nous.db.session.get_session_factory", MagicMock(return_value=MagicMock())
    )
    monkeypatch.setattr(
        "nous.observability.emit_run_telemetry", MagicMock(return_value=None)
    )
    return stage


def _kwargs(stage: AsyncMock) -> dict[str, Any]:
    stage.assert_awaited_once()
    return stage.await_args.kwargs


def test_flag_defaults_to_false(_stub_stage: AsyncMock) -> None:
    result = CliRunner().invoke(cli, ["judge-eligibility"])
    assert result.exit_code == 0, result.output
    assert _kwargs(_stub_stage)["rejudge_nonstartup_signals"] is False


def test_flag_passes_through_when_set(_stub_stage: AsyncMock) -> None:
    result = CliRunner().invoke(
        cli, ["judge-eligibility", "--rejudge-nonstartup-signals"]
    )
    assert result.exit_code == 0, result.output
    assert _kwargs(_stub_stage)["rejudge_nonstartup_signals"] is True


def test_limit_still_plumbs_alongside_flag(_stub_stage: AsyncMock) -> None:
    result = CliRunner().invoke(
        cli,
        ["judge-eligibility", "--limit", "5", "--rejudge-nonstartup-signals"],
    )
    assert result.exit_code == 0, result.output
    kwargs = _kwargs(_stub_stage)
    assert kwargs["limit"] == 5
    assert kwargs["rejudge_nonstartup_signals"] is True
