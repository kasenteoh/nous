"""Non-DB tests for compute-completeness: CLI wiring (the scorer itself is
covered by test_completeness.py; the stage behavior by
test_compute_completeness_db.py)."""

from __future__ import annotations

from click.testing import CliRunner

from nous.cli import cli


def test_cli_registered_with_limit_option() -> None:
    result = CliRunner().invoke(cli, ["compute-completeness", "--help"])
    assert result.exit_code == 0
    assert "--limit" in result.output
