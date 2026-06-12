"""Unit tests for observability helpers — no DATABASE_URL required.

These run in every environment (local dev, CI without DB, etc.).
"""

from __future__ import annotations

import pathlib

import pytest

from nous.observability import write_step_summary
from nous.pipeline.db_stats import DbStatsSummary, TableSize


def test_write_step_summary_honors_monkeypatched_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """write_step_summary appends to the file path from GITHUB_STEP_SUMMARY."""
    summary_file = tmp_path / "step_summary.md"

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    write_step_summary("## Test\nHello from test\n")
    write_step_summary("Second line\n")

    content = summary_file.read_text()

    assert "## Test" in content
    assert "Hello from test" in content
    assert "Second line" in content


def test_write_step_summary_silent_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_step_summary must not raise or create any file when env is absent."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # Should be a no-op — no exception raised.
    write_step_summary("This should be swallowed silently\n")


def test_db_stats_summary_sorted_descending() -> None:
    """DbStatsSummary.tables is always sorted largest-first (unit, no DB).

    Sorting is enforced by the field_validator on DbStatsSummary.tables, so
    supplying an *unsorted* list must produce a sorted result. This exercises
    the real sort path — deleting the validator must fail this test.
    """
    # Three tables supplied in *ascending* (wrong) order on purpose.
    tables_unsorted = [
        TableSize(name="small_table", bytes=1_000),
        TableSize(name="mid_table", bytes=5_000),
        TableSize(name="big_table", bytes=9_000),
    ]
    summary = DbStatsSummary(
        tables=tables_unsorted,
        total_bytes=15_000,
        cap_bytes=500 * 1024 * 1024,
        pct_of_cap=0.0,
        warn=False,
    )

    # Validator must have reordered the list to descending.
    byte_sizes = [t.bytes for t in summary.tables]
    assert byte_sizes == [9_000, 5_000, 1_000], (
        f"Tables not sorted descending by bytes: {byte_sizes}"
    )
