"""Tests for the adapter-health canary stage.

Pure unit tests — NO network, NO DB.  The real VC adapters are never called:
we inject a small registry of fake adapters (one healthy, one below floor, one
that raises) into ``run_adapter_health`` and assert the classification, the
adapter-failure isolation, the emitted annotations, and the serializable
summary projection.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import pytest

from nous.pipeline.adapter_health import (
    ADAPTER_FLOORS,
    DEFAULT_GLOBAL_FLOOR,
    AdapterHealth,
    AdapterHealthReport,
    build_summary,
    emit_adapter_health_annotations,
    floor_for,
    run_adapter_health,
)
from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios import PortfolioEntry

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeAdapter:
    """Stand-in for a PortfolioAdapter.

    Returns ``count`` canned entries; setting ``raises`` swaps the fetch path
    for an unconditional raise, exercising adapter-failure isolation.  Records
    whether ``fetch`` ran so a test can prove a sibling raise didn't prevent it.
    """

    firm: str
    count: int = 0
    raises: Exception | None = None
    fetched: bool = False

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        self.fetched = True
        if self.raises is not None:
            raise self.raises
        return [
            PortfolioEntry(
                firm=self.firm,
                name=f"{self.firm} co {i}",
                website=None,
                description=None,
                source_url=f"https://{self.firm}.example.com/portfolio",
            )
            for i in range(self.count)
        ]


class StubHomepageClient:
    """No-op HomepageClient stand-in; the fakes ignore it entirely."""

    async def __aenter__(self) -> StubHomepageClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _stub_client() -> HomepageClient:
    """Return a StubHomepageClient typed as HomepageClient for the call site."""
    return StubHomepageClient()  # type: ignore[return-value]


def _health(firm: str, count: int, floor: int, error: str | None = None) -> AdapterHealth:
    return AdapterHealth(firm=firm, count=count, floor=floor, error=error)


# ---------------------------------------------------------------------------
# floor_for — global default + per-adapter overrides
# ---------------------------------------------------------------------------


def test_floor_for_falls_back_to_global() -> None:
    assert floor_for("totally-unknown-firm") == DEFAULT_GLOBAL_FLOOR


def test_floor_for_honors_custom_global() -> None:
    assert floor_for("totally-unknown-firm", global_floor=42) == 42


def test_floor_for_uses_per_adapter_override() -> None:
    # yc has a large public directory; its override must beat the global floor.
    assert "yc" in ADAPTER_FLOORS
    assert floor_for("yc") == ADAPTER_FLOORS["yc"]
    # The override wins even when a different global floor is supplied.
    assert floor_for("yc", global_floor=1) == ADAPTER_FLOORS["yc"]


# ---------------------------------------------------------------------------
# AdapterHealth.healthy — boundary classification
# ---------------------------------------------------------------------------


def test_healthy_when_strictly_above_floor() -> None:
    assert _health("acme", count=11, floor=10).healthy is True


def test_unhealthy_when_at_floor() -> None:
    # count == floor is NOT healthy (must strictly exceed).
    assert _health("acme", count=10, floor=10).healthy is False


def test_unhealthy_when_below_floor() -> None:
    assert _health("acme", count=3, floor=10).healthy is False


def test_unhealthy_when_raised_even_if_count_zero() -> None:
    h = _health("acme", count=0, floor=10, error="RuntimeError('boom')")
    assert h.healthy is False


# ---------------------------------------------------------------------------
# run_adapter_health — sweep classification + failure isolation
# ---------------------------------------------------------------------------


async def test_sweep_classifies_healthy_below_and_raising() -> None:
    """One healthy, one below floor, one raising → exactly the latter two bad."""
    raiser = FakeAdapter("crash", raises=RuntimeError("site redesign"))
    healthy = FakeAdapter("healthy", count=50)
    low = FakeAdapter("low", count=2)
    registry = {"healthy": healthy, "low": low, "crash": raiser}

    report = await run_adapter_health(
        _stub_client(), adapters=registry, global_floor=10
    )

    assert len(report.adapters) == 3
    assert report.all_healthy is False

    bad = {a.firm for a in report.unhealthy}
    assert bad == {"low", "crash"}

    by_firm = {a.firm: a for a in report.adapters}
    assert by_firm["healthy"].healthy is True
    assert by_firm["healthy"].count == 50
    assert by_firm["low"].healthy is False
    assert by_firm["low"].count == 2
    # The raising adapter is recorded as a zero-count failure, not dropped.
    assert by_firm["crash"].count == 0
    assert by_firm["crash"].error is not None
    assert "site redesign" in by_firm["crash"].error


async def test_raising_adapter_is_isolated_others_still_run() -> None:
    """A raising adapter must NOT abort the sweep — siblings still fetch."""
    raiser = FakeAdapter("crash", raises=ValueError("boom"))
    after = FakeAdapter("zzz_after", count=99)  # sorts after 'crash'
    registry = {"crash": raiser, "zzz_after": after}

    report = await run_adapter_health(
        _stub_client(), adapters=registry, global_floor=10
    )

    # The adapter ordered AFTER the raiser still had fetch() invoked.
    assert after.fetched is True
    by_firm = {a.firm: a for a in report.adapters}
    assert by_firm["zzz_after"].healthy is True
    assert by_firm["crash"].error is not None


async def test_sweep_all_healthy() -> None:
    registry = {
        "a": FakeAdapter("a", count=20),
        "b": FakeAdapter("b", count=15),
    }
    report = await run_adapter_health(
        _stub_client(), adapters=registry, global_floor=10
    )
    assert report.all_healthy is True
    assert report.unhealthy == []
    assert report.total_entries == 35


async def test_sweep_respects_per_adapter_override() -> None:
    """A firm with a high override is below floor even with a healthy global count."""
    # 'yc' carries a large override; a count above the global floor but below
    # the override must still classify as unhealthy.
    yc_floor = ADAPTER_FLOORS["yc"]
    registry = {"yc": FakeAdapter("yc", count=yc_floor - 1)}
    report = await run_adapter_health(
        _stub_client(), adapters=registry, global_floor=10
    )
    assert report.all_healthy is False
    assert report.adapters[0].floor == yc_floor


# ---------------------------------------------------------------------------
# emit_adapter_health_annotations — annotation format
# ---------------------------------------------------------------------------


def _report(*adapters: AdapterHealth, global_floor: int = 10) -> AdapterHealthReport:
    return AdapterHealthReport(adapters=list(adapters), global_floor=global_floor)


def test_emit_annotations_all_healthy(capsys: pytest.CaptureFixture[str]) -> None:
    emit_adapter_health_annotations(_report(_health("a", 20, 10)))
    out = capsys.readouterr().out
    assert "above floor" in out
    assert "::warning::" not in out


def test_emit_annotations_below_floor_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_adapter_health_annotations(_report(_health("lightspeed", 3, 50)))
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "lightspeed" in out
    assert "3" in out
    assert "50" in out


def test_emit_annotations_raised_warns(capsys: pytest.CaptureFixture[str]) -> None:
    emit_adapter_health_annotations(
        _report(_health("kp", 0, 30, error="RuntimeError('redesign')"))
    )
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "kp" in out
    assert "FAILED" in out


def test_emit_annotations_mixed_only_bad_warn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_adapter_health_annotations(
        _report(
            _health("ok", 40, 10),
            _health("low", 1, 10),
            _health("crash", 0, 10, error="boom"),
        )
    )
    out = capsys.readouterr().out
    # Two bad adapters → two warnings; the healthy one isn't warned about.
    assert out.count("::warning::") == 2
    assert "low" in out
    assert "crash" in out


def test_step_summary_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    emit_adapter_health_annotations(
        _report(_health("ok", 40, 10), _health("low", 1, 10))
    )

    content = summary_file.read_text()
    assert "ok" in content
    assert "low" in content
    assert "below floor" in content


def test_step_summary_written_when_all_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    emit_adapter_health_annotations(_report(_health("ok", 40, 10)))

    content = summary_file.read_text()
    assert "ok" in content
    assert "above floor" in content


# ---------------------------------------------------------------------------
# build_summary — serializable projection for pipeline_runs.summary
# ---------------------------------------------------------------------------


def test_build_summary_counts_and_failures() -> None:
    report = _report(
        _health("ok", 40, 10),
        _health("low", 2, 10),
        _health("crash", 0, 10, error="RuntimeError('x')"),
        global_floor=10,
    )
    summary = build_summary(report)

    assert summary.adapters_checked == 3
    assert summary.adapters_healthy == 1
    assert summary.adapters_unhealthy == 2
    assert summary.total_entries == 42
    assert summary.global_floor == 10
    assert summary.counts == {"ok": 40, "low": 2, "crash": 0}
    assert summary.floors == {"ok": 10, "low": 10, "crash": 10}
    assert summary.failures == {"crash": "RuntimeError('x')"}
    assert set(summary.below_floor) == {"low", "crash"}
    # Must be JSON-serializable for the pipeline_runs.summary jsonb column.
    assert "below_floor" in summary.model_dump(mode="json")
