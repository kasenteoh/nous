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
    DEFAULT_NEWS_FLOOR,
    NEWS_FEEDS,
    AdapterHealth,
    AdapterHealthReport,
    build_summary,
    emit_adapter_health_annotations,
    floor_for,
    news_floor_for,
    run_adapter_health,
)
from nous.sources.crunchbase_news import CB_NEWS_FEED
from nous.sources.geekwire import GEEKWIRE_FUNDING_FEED
from nous.sources.homepage import HomepageClient
from nous.sources.news import NewsClient, RobotsBlockedError
from nous.sources.prnewswire import PRNEWSWIRE_VC_FEED
from nous.sources.siliconangle import SILICONANGLE_FEED
from nous.sources.techcrunch import TC_FUNDING_FEED
from nous.sources.vc_portfolios import PortfolioEntry
from nous.sources.venturebeat import VENTUREBEAT_FEED

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


# ---------------------------------------------------------------------------
# News-feed probing — the broad funding feeds get the same canary treatment
# ---------------------------------------------------------------------------


class StubNewsClient(NewsClient):
    """NewsClient whose fetch_text serves canned bodies (or raises) per URL.

    Subclasses the real client so ``_parse_rss`` is the production parser —
    the probe's fetch is the only thing stubbed. Never entered as a context
    manager: fetch_text is overridden and _parse_rss is transport-free.
    """

    def __init__(self, responses: dict[str, str | Exception]) -> None:
        super().__init__(user_agent="nous-test test@example.com")
        self._responses = responses

    async def fetch_text(self, url: str) -> str:
        outcome = self._responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# Items carry no pubDate on purpose: undated entries survive any lookback
# cutoff, keeping these tests independent of the wall clock.
_ALIVE_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Feed</title>
  <item><title>Post one</title><link>https://feed.example.com/one</link></item>
  <item><title>Post two</title><link>https://feed.example.com/two</link></item>
</channel></rss>
"""

_EMPTY_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Feed</title></channel></rss>
"""


def test_news_floor_for_defaults_to_news_floor() -> None:
    assert news_floor_for("totally-unknown-feed") == DEFAULT_NEWS_FLOOR


def test_news_feeds_registry_matches_broad_sweep_sources() -> None:
    """Every ingest-news broad feed is canary-covered, keyed by its
    discovered_via slug, probing the exact URL the fetcher uses."""
    assert NEWS_FEEDS == {
        "techcrunch": TC_FUNDING_FEED,
        "siliconangle": SILICONANGLE_FEED,
        "prnewswire": PRNEWSWIRE_VC_FEED,
        "crunchbase_news": CB_NEWS_FEED,
        "venturebeat": VENTUREBEAT_FEED,
        "geekwire": GEEKWIRE_FUNDING_FEED,
    }


async def test_news_feeds_probed_and_classified() -> None:
    """Feeds report under news:<slug>: raw items > 0 healthy, an empty feed
    unhealthy, and a transport failure recorded as an error (NOT hidden as
    [] the way the ingest-path fetchers deliberately do)."""
    news_client = StubNewsClient(
        {
            "https://ok.example.com/feed": _ALIVE_FEED_XML,
            "https://dead.example.com/feed": _EMPTY_FEED_XML,
            "https://blocked.example.com/feed": RobotsBlockedError(
                "robots.txt disallows: https://blocked.example.com/feed"
            ),
        }
    )

    report = await run_adapter_health(
        _stub_client(),
        adapters={},
        news_client=news_client,
        news_feeds={
            "okfeed": "https://ok.example.com/feed",
            "deadfeed": "https://dead.example.com/feed",
            "blockedfeed": "https://blocked.example.com/feed",
        },
    )

    by_key = {a.firm: a for a in report.adapters}
    assert set(by_key) == {"news:okfeed", "news:deadfeed", "news:blockedfeed"}

    assert by_key["news:okfeed"].healthy is True
    assert by_key["news:okfeed"].count == 2
    assert by_key["news:okfeed"].floor == DEFAULT_NEWS_FLOOR

    # Zero raw items sits AT the floor (0) — not healthy: an unfiltered parse
    # of a live feed always has items, so zero means dead/frozen/format-drift.
    assert by_key["news:deadfeed"].healthy is False
    assert by_key["news:deadfeed"].error is None

    assert by_key["news:blockedfeed"].healthy is False
    assert by_key["news:blockedfeed"].error is not None
    assert "robots.txt disallows" in by_key["news:blockedfeed"].error


async def test_news_feeds_skipped_without_news_client() -> None:
    """No news_client (the default) means a VC-only sweep — existing callers
    and tests see identical behavior."""
    report = await run_adapter_health(
        _stub_client(),
        adapters={"acme": FakeAdapter(firm="acme", count=20)},
        news_feeds={"okfeed": "https://ok.example.com/feed"},
    )
    assert [a.firm for a in report.adapters] == ["acme"]


async def test_mixed_sweep_reports_firms_and_feeds_together() -> None:
    """Firms and feeds land in one report/summary with disjoint keys."""
    news_client = StubNewsClient({"https://acme.example.com/feed": _ALIVE_FEED_XML})

    report = await run_adapter_health(
        _stub_client(),
        adapters={"acme": FakeAdapter(firm="acme", count=20)},
        news_client=news_client,
        # Same bare slug as the firm — the news: prefix must prevent collision.
        news_feeds={"acme": "https://acme.example.com/feed"},
    )
    summary = build_summary(report)
    assert summary.counts == {"acme": 20, "news:acme": 2}
    assert summary.adapters_checked == 2
    assert summary.adapters_unhealthy == 0
