"""Unit tests for compute-themes helpers — no DB, no scikit-learn, no LLM.

The stage's DB behavior (clustering→naming→persistence, TTL gate, slug
stability, replace semantics, funding math against real rows) lives in
test_compute_themes_db.py; this file pins the pure functions those flows
depend on, the deterministic fake-clusterer contract shared by both files,
and the CLI registration.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from click.testing import CliRunner

from nous.cli import cli
from nous.pipeline.compute_themes import (
    CENTROID_MATCH_THRESHOLD,
    MAX_K,
    _unique_slug,
    centroid_of,
    choose_k,
    compute_funding_metrics,
    cosine_similarity,
    funding_windows,
    match_clusters_to_themes,
    quarter_start,
    theme_slug,
    unit_normalize,
)


class FakeClusterer:
    """Deterministic clusterer: label = argmax(vector) mod k.

    A pure function of the input, so re-clustering identical vectors yields
    identical labels — the property the slug-stability tests lean on. Tests
    construct member embeddings as (perturbed) basis vectors, making the
    intended grouping explicit: everything peaking on dimension d lands in
    cluster d % k. Also records calls so tests can assert k and input size.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def cluster(self, vectors: list[list[float]], k: int) -> list[int]:
        self.calls.append((len(vectors), k))
        return [
            max(range(len(vec)), key=lambda i: vec[i]) % k for vec in vectors
        ]


# ---------------------------------------------------------------------------
# k heuristic
# ---------------------------------------------------------------------------


def test_choose_k_rule_of_thumb() -> None:
    assert choose_k(8) == 2  # sqrt(4) — the minimum industry size
    assert choose_k(50) == 5  # sqrt(25)
    assert choose_k(200) == 10  # sqrt(100) — exactly the cap


def test_choose_k_clamps() -> None:
    assert choose_k(3) == 2  # floor: always at least 2 clusters
    assert choose_k(5000) == MAX_K  # ceiling: themes stay coarse


# ---------------------------------------------------------------------------
# Vector math
# ---------------------------------------------------------------------------


def test_unit_normalize() -> None:
    vec = unit_normalize([3.0, 4.0])
    assert vec == [0.6, 0.8]
    # Zero vectors pass through rather than dividing by zero.
    assert unit_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_centroid_of_is_unit_mean() -> None:
    centroid = centroid_of([[1.0, 0.0], [0.0, 1.0]])
    expected = 1.0 / (2.0**0.5)
    assert abs(centroid[0] - expected) < 1e-12
    assert abs(centroid[1] - expected) < 1e-12


def test_cosine_similarity() -> None:
    assert abs(cosine_similarity([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-12
    assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-12
    # Scale-invariant (inputs need not be unit length).
    assert abs(cosine_similarity([2.0, 0.0], [5.0, 0.0]) - 1.0) < 1e-12
    # Zero vector -> 0, not NaN.
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------


def test_theme_slug_basics() -> None:
    assert theme_slug("AI Code Review") == "ai-code-review"
    assert theme_slug("Café Analytics") == "cafe-analytics"
    assert theme_slug("  Fleet — telematics!  ") == "fleet-telematics"


def test_theme_slug_keeps_corporate_suffix_words() -> None:
    # util.slugify.slugify would strip the "Co" — theme names must not.
    assert theme_slug("Payroll Co-pilots") == "payroll-co-pilots"


def test_theme_slug_never_empty() -> None:
    assert theme_slug("!!!") == "theme"
    assert theme_slug("") == "theme"


def test_unique_slug_suffixes_and_reserves() -> None:
    taken = {"ai-agents"}
    assert _unique_slug("ai-agents", taken) == "ai-agents-2"
    # The suffixed slug is reserved too, so a third collision advances.
    assert _unique_slug("ai-agents", taken) == "ai-agents-3"
    assert _unique_slug("fresh", taken) == "fresh"
    assert "fresh" in taken


# ---------------------------------------------------------------------------
# Funding windows + growth math
# ---------------------------------------------------------------------------


def test_quarter_start() -> None:
    assert quarter_start(date(2026, 7, 11)) == date(2026, 7, 1)
    assert quarter_start(date(2026, 1, 1)) == date(2026, 1, 1)
    assert quarter_start(date(2026, 12, 31)) == date(2026, 10, 1)


def test_funding_windows_mid_year() -> None:
    prior_start, recent_start, recent_end = funding_windows(date(2026, 7, 11))
    # Recent = Q1+Q2 2026 (the two most recent COMPLETE quarters); the
    # in-progress Q3 is excluded so partial windows never compare to full ones.
    assert recent_end == date(2026, 7, 1)
    assert recent_start == date(2026, 1, 1)
    assert prior_start == date(2025, 7, 1)


def test_funding_windows_crosses_year_boundary() -> None:
    prior_start, recent_start, recent_end = funding_windows(date(2026, 2, 15))
    assert recent_end == date(2026, 1, 1)
    assert recent_start == date(2025, 7, 1)
    assert prior_start == date(2025, 1, 1)


def test_compute_funding_metrics_sums_and_growth() -> None:
    today = date(2026, 7, 11)
    rounds: list[tuple[date | None, Decimal | None]] = [
        (date(2026, 3, 1), Decimal("10000000")),  # recent (Q1 2026)
        (date(2026, 5, 15), Decimal("5000000")),  # recent (Q2 2026)
        (date(2025, 8, 1), Decimal("5000000")),  # prior (Q3 2025)
        (date(2026, 7, 5), Decimal("99000000")),  # current quarter: excluded
        (date(2024, 1, 1), Decimal("77000000")),  # before the horizon: excluded
        (None, Decimal("1000000")),  # undated: can't be placed — skipped
        (date(2026, 2, 2), None),  # unamounted: nothing to sum — skipped
    ]
    recent, prior, growth = compute_funding_metrics(rounds, today=today)
    assert recent == Decimal("15000000")
    assert prior == Decimal("5000000")
    assert growth == Decimal("2.0000")  # (15M - 5M) / 5M


def test_compute_funding_metrics_zero_prior_gives_null_growth() -> None:
    recent, prior, growth = compute_funding_metrics(
        [(date(2026, 3, 1), Decimal("1000000"))], today=date(2026, 7, 11)
    )
    assert recent == Decimal("1000000")
    assert prior == Decimal("0")
    assert growth is None  # undefined over a zero base — never an infinity


def test_compute_funding_metrics_negative_growth() -> None:
    recent, prior, growth = compute_funding_metrics(
        [
            (date(2026, 3, 1), Decimal("2000000")),
            (date(2025, 9, 1), Decimal("8000000")),
        ],
        today=date(2026, 7, 11),
    )
    assert recent == Decimal("2000000")
    assert prior == Decimal("8000000")
    assert growth == Decimal("-0.7500")


# ---------------------------------------------------------------------------
# Centroid matching (slug stability)
# ---------------------------------------------------------------------------


def _basis(axis: int, dim: int = 4) -> list[float]:
    vec = [0.0] * dim
    vec[axis] = 1.0
    return vec


def test_match_identical_centroids() -> None:
    theme_id = uuid4()
    assigned = match_clusters_to_themes([_basis(0)], [(theme_id, _basis(0))])
    assert assigned == {0: theme_id}


def test_match_below_threshold_is_new_theme() -> None:
    # cos(45°) ≈ 0.707 < 0.9 — drifted too far, treated as new content.
    drifted = unit_normalize([1.0, 1.0, 0.0, 0.0])
    assert match_clusters_to_themes([drifted], [(uuid4(), _basis(0))]) == {}


def test_match_just_above_threshold_keeps_slug() -> None:
    theme_id = uuid4()
    # cosine ≈ 0.91 to the basis — incremental drift, inside the tolerance.
    near = [0.91, (1 - 0.91**2) ** 0.5, 0.0, 0.0]
    sim = cosine_similarity(near, _basis(0))
    assert CENTROID_MATCH_THRESHOLD < sim < 1.0
    assert match_clusters_to_themes([near], [(theme_id, _basis(0))]) == {0: theme_id}


def test_match_is_one_to_one_greedy_by_similarity() -> None:
    theme_id = uuid4()
    exact = _basis(0)
    close = unit_normalize([0.97, 0.05, 0.0, 0.0])
    # Both clusters clear 0.9 against the single theme; the closer one wins,
    # the other becomes a new theme.
    assigned = match_clusters_to_themes([close, exact], [(theme_id, _basis(0))])
    assert assigned == {1: theme_id}


def test_match_prefers_closest_previous_theme() -> None:
    exact_id, close_id = uuid4(), uuid4()
    assigned = match_clusters_to_themes(
        [_basis(0)],
        [
            (close_id, unit_normalize([0.95, 0.1, 0.0, 0.0])),
            (exact_id, _basis(0)),
        ],
    )
    assert assigned == {0: exact_id}


# ---------------------------------------------------------------------------
# Fake clusterer contract + CLI
# ---------------------------------------------------------------------------


def test_fake_clusterer_is_deterministic_and_groups_by_peak() -> None:
    clusterer = FakeClusterer()
    vectors = [_basis(0), _basis(1), _basis(0)]
    first = clusterer.cluster(vectors, 2)
    second = clusterer.cluster(vectors, 2)
    assert first == second == [0, 1, 0]
    assert clusterer.calls == [(3, 2), (3, 2)]


def test_compute_themes_cli_is_registered_with_bounded_default() -> None:
    result = CliRunner().invoke(cli, ["compute-themes", "--help"])
    assert result.exit_code == 0
    assert "--limit" in result.output
    assert "100" in result.output  # the documented per-run LLM cluster cap
    assert "--ttl-days" in result.output
    assert "25" in result.output
    assert "--force" in result.output
