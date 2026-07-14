"""Unit tests for compute-momentum's pure core — no DB.

Pins the arithmetic the whole stage rests on: each component's [0, 1] mapping
(0.5 = flat), the news smoothing/clip that keeps a 0→N spike finite and bounded,
the weight-renormalized combine (a missing signal drops out rather than drags),
the all-absent → NULL rule, the pre-worded "why" chips, and determinism. The
stage's DB behavior (shown cohort, snapshot windows, idempotent writes) lives in
test_compute_momentum_db.py. Plus the CLI registration.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from click.testing import CliRunner

from nous.cli import cli
from nous.db.models import CompanySnapshot
from nous.pipeline.compute_momentum import (
    COMPONENT_WEIGHTS,
    NEWS_RATIO_CAP,
    W_FUNDING,
    W_HEADCOUNT,
    W_NEWS,
    build_why,
    combine,
    funding_component,
    funding_days_since,
    headcount_component,
    headcount_growth,
    news_component,
    news_norm_from_ratio,
    news_ratio,
    score_company,
)

AS_OF = date(2026, 7, 13)  # a Monday


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_weights_sum_and_ordering() -> None:
    # News leads, headcount trails; the trio is the combine denominator.
    assert W_NEWS > W_FUNDING > W_HEADCOUNT
    assert COMPONENT_WEIGHTS == {
        "news": W_NEWS,
        "funding": W_FUNDING,
        "headcount": W_HEADCOUNT,
    }


# ---------------------------------------------------------------------------
# News component
# ---------------------------------------------------------------------------


def test_news_norm_from_ratio_anchors() -> None:
    assert news_norm_from_ratio(1.0) == pytest.approx(0.5)  # flat
    assert news_norm_from_ratio(NEWS_RATIO_CAP) == pytest.approx(1.0)  # max up
    assert news_norm_from_ratio(1.0 / NEWS_RATIO_CAP) == pytest.approx(0.0)  # max down


def test_news_component_flat_is_half() -> None:
    assert news_component([5, 5], [5, 5]) == pytest.approx(0.5)


def test_news_component_accelerating_above_half() -> None:
    out = news_component([10, 10], [2, 2])
    assert out is not None and out > 0.5


def test_news_component_decelerating_below_half() -> None:
    out = news_component([1], [10])
    assert out is not None and out < 0.5


def test_news_component_zero_to_n_is_finite_not_infinite() -> None:
    # 0 baseline -> the +K smoothing keeps the ratio finite (would be ∞ raw).
    out = news_component([3], [0])
    assert out is not None and 0.5 < out < 1.0


def test_news_component_spike_clamps_to_one() -> None:
    # A 100x blowup saturates at the CAP, never overshoots 1.0.
    assert news_component([100], [1]) == pytest.approx(1.0)


def test_news_component_absent_when_a_window_is_empty() -> None:
    assert news_component([], [5]) is None  # no recent snapshots
    assert news_component([5], []) is None  # no baseline snapshots
    assert news_component([], []) is None


def test_news_ratio_is_clipped_to_band() -> None:
    assert news_ratio([100], [1]) == pytest.approx(NEWS_RATIO_CAP)
    assert news_ratio([0], [100]) == pytest.approx(1.0 / NEWS_RATIO_CAP)


# ---------------------------------------------------------------------------
# Funding component
# ---------------------------------------------------------------------------


def test_funding_component_fresh_raise_is_one() -> None:
    assert funding_component(AS_OF, AS_OF) == pytest.approx(1.0)


def test_funding_component_one_tau_is_one_over_e() -> None:
    d = AS_OF - _days(180)
    assert funding_component(d, AS_OF) == pytest.approx(math.exp(-1.0), rel=1e-6)


def test_funding_component_decays_monotonically() -> None:
    at_30 = funding_component(AS_OF - _days(30), AS_OF)
    at_90 = funding_component(AS_OF - _days(90), AS_OF)
    at_365 = funding_component(AS_OF - _days(365), AS_OF)
    assert at_30 is not None and at_90 is not None and at_365 is not None
    assert at_30 > at_90 > at_365


def test_funding_component_absent_without_date() -> None:
    assert funding_component(None, AS_OF) is None


def test_funding_component_future_date_clamps_to_one() -> None:
    assert funding_component(AS_OF + _days(10), AS_OF) == pytest.approx(1.0)


def test_funding_days_since() -> None:
    assert funding_days_since(AS_OF - _days(14), AS_OF) == 14
    assert funding_days_since(AS_OF + _days(5), AS_OF) == 0  # never negative
    assert funding_days_since(None, AS_OF) is None


# ---------------------------------------------------------------------------
# Headcount component
# ---------------------------------------------------------------------------


def test_headcount_component_growth_above_half() -> None:
    assert headcount_component(120.0, 100.0) == pytest.approx(0.6)  # +20%


def test_headcount_component_doubling_is_one() -> None:
    assert headcount_component(200.0, 100.0) == pytest.approx(1.0)


def test_headcount_component_shrink_below_half_and_clips() -> None:
    assert headcount_component(50.0, 100.0) == pytest.approx(0.25)  # -50%
    assert headcount_component(0.0, 100.0) == pytest.approx(0.0)  # clip floor


def test_headcount_component_absent_when_a_reading_missing_or_zero_base() -> None:
    assert headcount_component(None, 100.0) is None
    assert headcount_component(100.0, None) is None
    assert headcount_component(100.0, 0.0) is None  # growth undefined over 0


def test_headcount_growth() -> None:
    assert headcount_growth(150.0, 100.0) == pytest.approx(0.5)
    assert headcount_growth(None, 100.0) is None
    assert headcount_growth(100.0, 0.0) is None


# ---------------------------------------------------------------------------
# combine: weight-renormalized mean over present components
# ---------------------------------------------------------------------------


def test_combine_all_present_flat_is_half_full_confidence() -> None:
    score, conf = combine({"news": 0.5, "funding": 0.5, "headcount": 0.5})
    assert score == pytest.approx(0.5)
    assert conf == pytest.approx(1.0)


def test_combine_news_only_equals_news_norm() -> None:
    # A missing component drops out — a news-only score IS the news norm, not a
    # value dragged toward 0 by absent signals.
    score, conf = combine({"news": 0.82, "funding": None, "headcount": None})
    assert score == pytest.approx(0.82)
    assert conf == pytest.approx(W_NEWS / (W_NEWS + W_FUNDING + W_HEADCOUNT))


def test_combine_renormalizes_over_present_pair() -> None:
    score, conf = combine({"news": 1.0, "funding": 0.0, "headcount": None})
    assert score == pytest.approx((W_NEWS * 1.0) / (W_NEWS + W_FUNDING))
    assert conf == pytest.approx((W_NEWS + W_FUNDING) / sum(COMPONENT_WEIGHTS.values()))


def test_combine_all_absent_is_null() -> None:
    assert combine({"news": None, "funding": None, "headcount": None}) == (None, 0.0)


def test_combine_is_deterministic() -> None:
    payload = {"news": 0.7, "funding": 0.3, "headcount": 0.55}
    assert combine(payload) == combine(payload)


# ---------------------------------------------------------------------------
# build_why: pre-worded chips
# ---------------------------------------------------------------------------


def test_build_why_reports_acceleration_then_recency_then_team() -> None:
    why = build_why(
        news_recent=8.0,
        news_ratio_value=2.8,  # >= 1.5 -> % form
        funding_days=20,
        headcount_growth_value=0.4,
    )
    assert why == ["news +180%", "raised 3wks ago", "+40% team"]


def test_build_why_news_volume_when_not_strongly_accelerating() -> None:
    why = build_why(
        news_recent=5.0,
        news_ratio_value=1.1,  # < 1.5 -> volume form
        funding_days=None,
        headcount_growth_value=None,
    )
    assert why == ["5 news mentions"]


def test_build_why_funding_recency_wording() -> None:
    assert build_why(
        news_recent=None, news_ratio_value=None, funding_days=3,
        headcount_growth_value=None,
    ) == ["raised this week"]
    assert build_why(
        news_recent=None, news_ratio_value=None, funding_days=100,
        headcount_growth_value=None,
    ) == ["raised 3mo ago"]


def test_build_why_team_shrink_and_flat() -> None:
    assert build_why(
        news_recent=None, news_ratio_value=None, funding_days=None,
        headcount_growth_value=-0.1,
    ) == ["-10% team"]
    # Exactly flat -> no team chip (nothing notable to say).
    assert build_why(
        news_recent=None, news_ratio_value=None, funding_days=None,
        headcount_growth_value=0.0,
    ) == []


def test_build_why_all_absent_is_empty() -> None:
    assert build_why(
        news_recent=None, news_ratio_value=None, funding_days=None,
        headcount_growth_value=None,
    ) == []


# ---------------------------------------------------------------------------
# score_company: pure over an in-memory snapshot series (no DB)
# ---------------------------------------------------------------------------


def test_score_company_funding_only_cold_start() -> None:
    # No snapshots at all, but a recent raise -> a real (funding-only) score.
    result = score_company(
        snapshots=[], latest_round_date=AS_OF - _days(21), as_of=AS_OF
    )
    assert result.score is not None and result.score > 0.5
    assert result.why == ["raised 3wks ago"]


def test_score_company_all_absent_is_null() -> None:
    result = score_company(snapshots=[], latest_round_date=None, as_of=AS_OF)
    assert result.score is None
    assert result.why == []


def test_score_company_rising_news_scores_above_half() -> None:
    # Recent weeks hot, baseline quiet -> accelerating -> > 0.5, news chip.
    snaps = [
        _snap(AS_OF, news=12),
        _snap(AS_OF - _days(7), news=10),
        _snap(AS_OF - _days(28), news=2),
        _snap(AS_OF - _days(56), news=1),
    ]
    result = score_company(snapshots=snaps, latest_round_date=None, as_of=AS_OF)
    assert result.score is not None and result.score > 0.5
    assert any("news" in chip for chip in result.why)


def test_score_company_is_deterministic() -> None:
    snaps = [
        _snap(AS_OF, news=9, lo=40, hi=60),
        _snap(AS_OF - _days(7), news=8),
        _snap(AS_OF - _days(28), news=3),
        _snap(AS_OF - _days(63), news=2, lo=20, hi=30),
    ]
    a = score_company(
        snapshots=snaps, latest_round_date=AS_OF - _days(40), as_of=AS_OF
    )
    b = score_company(
        snapshots=snaps, latest_round_date=AS_OF - _days(40), as_of=AS_OF
    )
    assert a.score == b.score
    assert a.why == b.why


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def test_compute_momentum_cli_is_registered() -> None:
    result = CliRunner().invoke(cli, ["compute-momentum", "--help"])
    assert result.exit_code == 0
    assert "--as-of-week" in result.output


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _days(n: int) -> timedelta:
    return timedelta(days=n)


def _snap(
    captured_week: date,
    *,
    news: int,
    lo: int | None = None,
    hi: int | None = None,
) -> CompanySnapshot:
    """An in-memory CompanySnapshot (never flushed) for pure scoring tests."""
    return CompanySnapshot(
        captured_week=captured_week,
        news_count_30d=news,
        employee_count_min=lo,
        employee_count_max=hi,
    )
