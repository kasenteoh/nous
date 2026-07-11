"""CLI plumbing test for ``enrich-companies --redescribe-outdated``.

The stage BEHAVIOR (selection, stamping, idempotency) is covered by
test_enrich_companies.py. This covers only the wiring: the flag exists,
defaults to off, appends a redescribe pass bounded by --limit after the
normal pass, and is mutually exclusive with --backfill-missing-taxonomy.
No DATABASE_URL needed — the stages, the session factory, and the
observability helpers are all stubbed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from nous.cli import cli
from nous.pipeline.enrich_companies import EnrichSummary, RedescribeSummary


class _FakeSessionCM:
    async def __aenter__(self) -> MagicMock:
        return MagicMock()

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture()
def _stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    enrich = AsyncMock(return_value=EnrichSummary())
    redescribe = AsyncMock(return_value=RedescribeSummary())
    # Patch at the SOURCE modules — the command imports these names locally
    # inside its coroutine, so lookups resolve against the live modules.
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.run_enrich_companies", enrich
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.run_redescribe_outdated", redescribe
    )
    monkeypatch.setattr(
        "nous.db.session.AsyncSessionLocal",
        MagicMock(return_value=_FakeSessionCM()),
    )
    monkeypatch.setattr(
        "nous.observability.record_pipeline_run", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "nous.observability.emit_run_telemetry", MagicMock(return_value=None)
    )
    return {"enrich": enrich, "redescribe": redescribe}


def test_flag_defaults_off(_stubs: dict[str, AsyncMock]) -> None:
    result = CliRunner().invoke(cli, ["enrich-companies", "--limit", "7"])
    assert result.exit_code == 0, result.output
    _stubs["enrich"].assert_awaited_once()
    _stubs["redescribe"].assert_not_awaited()


def test_flag_appends_redescribe_pass_with_same_limit(
    _stubs: dict[str, AsyncMock],
) -> None:
    result = CliRunner().invoke(
        cli, ["enrich-companies", "--limit", "7", "--redescribe-outdated"]
    )
    assert result.exit_code == 0, result.output
    _stubs["enrich"].assert_awaited_once()
    _stubs["redescribe"].assert_awaited_once()
    kwargs: dict[str, Any] = _stubs["redescribe"].await_args.kwargs
    assert kwargs["max_companies"] == 7


def test_flag_conflicts_with_backfill_mode(_stubs: dict[str, AsyncMock]) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "enrich-companies",
            "--redescribe-outdated",
            "--backfill-missing-taxonomy",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    _stubs["enrich"].assert_not_awaited()
    _stubs["redescribe"].assert_not_awaited()
