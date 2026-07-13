"""Unit tests for compute-map-positions helpers — no DB, no scikit-learn.

The stage's DB behavior (per-industry fetch, threshold, TTL gate, write +
idempotence) lives in test_compute_map_positions_db.py; this file pins the pure
projection/normalization core those flows depend on — sign pinning (the thing
that keeps min-max from emitting mirror-image maps), the [0, 1] min-max, the
degenerate-axis fallback, determinism — plus the deterministic FakeProjector
contract shared by both files, and the CLI registration.
"""

from __future__ import annotations

from click.testing import CliRunner

from nous.cli import cli
from nous.pipeline.compute_map_positions import (
    MIN_MAP_COMPANIES,
    _minmax_axis,
    _pin_sign,
    finalize_coords,
    unit_normalize,
)


class FakeProjector:
    """Deterministic projector: returns the first 2 dims of each vector.

    A pure function of the input, so re-projecting identical vectors yields
    identical scores — the property the determinism/idempotence tests lean on.
    Records calls so tests can assert the cohort size and n_components.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def project(
        self, vectors: list[list[float]], n_components: int
    ) -> list[list[float]]:
        self.calls.append((len(vectors), n_components))
        return [[vec[0], vec[1]] for vec in vectors]


# ---------------------------------------------------------------------------
# unit_normalize
# ---------------------------------------------------------------------------


def test_unit_normalize() -> None:
    assert unit_normalize([3.0, 4.0]) == [0.6, 0.8]
    # Zero vectors pass through rather than dividing by zero.
    assert unit_normalize([0.0, 0.0]) == [0.0, 0.0]


# ---------------------------------------------------------------------------
# _minmax_axis
# ---------------------------------------------------------------------------


def test_minmax_axis_spans_zero_to_one() -> None:
    out = _minmax_axis([2.0, 4.0, 6.0])
    assert out == [0.0, 0.5, 1.0]  # endpoints hit exactly 0.0 and 1.0


def test_minmax_axis_constant_maps_to_half() -> None:
    # Degenerate (all-equal) axis -> 0.5, never a divide-by-zero.
    assert _minmax_axis([7.0, 7.0, 7.0]) == [0.5, 0.5, 0.5]
    assert _minmax_axis([3.0]) == [0.5]  # single sample is constant too


# ---------------------------------------------------------------------------
# _pin_sign (the determinism keystone)
# ---------------------------------------------------------------------------


def test_pin_sign_forces_largest_magnitude_positive() -> None:
    # Axis 0: largest |value| is -5 (idx 1) -> whole axis negates.
    # Axis 1: largest |value| is 4 (idx 0), already positive -> unchanged.
    signed = _pin_sign([[2.0, 4.0], [-5.0, 1.0], [1.0, -3.0]])
    assert signed == [[-2.0, 4.0], [5.0, 1.0], [-1.0, -3.0]]


def test_pin_sign_ties_go_to_lowest_index() -> None:
    # Two samples tie on |value|=5; the strict-greater scan keeps the first
    # (idx 0), which is negative -> the axis flips to make idx 0 positive.
    signed = _pin_sign([[-5.0], [5.0]])
    assert signed == [[5.0], [-5.0]]


# ---------------------------------------------------------------------------
# finalize_coords
# ---------------------------------------------------------------------------


_RAW = [[2.0, -1.0], [-3.0, 4.0], [1.0, 0.5], [0.0, -2.0]]


def test_finalize_coords_in_unit_box_with_exact_endpoints() -> None:
    coords = finalize_coords(_RAW)
    xs = [x for x, _ in coords]
    ys = [y for _, y in coords]
    assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in coords)
    # A non-degenerate axis fills the box exactly.
    assert min(xs) == 0.0 and max(xs) == 1.0
    assert min(ys) == 0.0 and max(ys) == 1.0


def test_finalize_coords_is_sign_invariant_global_mirror() -> None:
    # PCA sign is arbitrary; a globally mirrored projection must normalize to
    # IDENTICAL coords (the sign pin removes the ambiguity min-max would encode).
    mirrored = [[-x, -y] for x, y in _RAW]
    assert finalize_coords(_RAW) == finalize_coords(mirrored)


def test_finalize_coords_is_sign_invariant_per_axis() -> None:
    # Each PCA axis can independently flip; pinning must absorb a single-axis
    # flip too, not just a global one.
    flip_x = [[-x, y] for x, y in _RAW]
    flip_y = [[x, -y] for x, y in _RAW]
    base = finalize_coords(_RAW)
    assert finalize_coords(flip_x) == base
    assert finalize_coords(flip_y) == base


def test_finalize_coords_is_deterministic() -> None:
    assert finalize_coords(_RAW) == finalize_coords(_RAW)


def test_finalize_coords_degenerate_x_axis_is_half() -> None:
    # All samples share an x-score -> every map_x collapses to 0.5; y still
    # spreads across [0, 1].
    coords = finalize_coords([[5.0, 0.0], [5.0, 2.0], [5.0, 4.0]])
    assert [x for x, _ in coords] == [0.5, 0.5, 0.5]
    assert [y for _, y in coords] == [0.0, 0.5, 1.0]


def test_finalize_coords_single_sample_is_center() -> None:
    # One-company cohort: both axes degenerate -> dead center.
    assert finalize_coords([[3.0, 9.0]]) == [(0.5, 0.5)]


def test_finalize_coords_empty() -> None:
    assert finalize_coords([]) == []


# ---------------------------------------------------------------------------
# FakeProjector contract + constants
# ---------------------------------------------------------------------------


def test_fake_projector_is_deterministic_and_records_calls() -> None:
    proj = FakeProjector()
    vectors = [[1.0, 2.0, 9.0], [3.0, 4.0, 0.0]]
    first = proj.project(vectors, 2)
    second = proj.project(vectors, 2)
    assert first == second == [[1.0, 2.0], [3.0, 4.0]]
    assert proj.calls == [(2, 2), (2, 2)]


def test_min_map_companies_floor() -> None:
    assert MIN_MAP_COMPANIES == 5


def test_compute_map_positions_cli_is_registered() -> None:
    result = CliRunner().invoke(cli, ["compute-map-positions", "--help"])
    assert result.exit_code == 0
    assert "--ttl-days" in result.output
    assert "25" in result.output  # the documented per-industry TTL default
    assert "--force" in result.output
