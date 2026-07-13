"""compute-map-positions: per-industry PCA(2) projection of company embeddings
into normalized 2D coords for the static /map/[industry] SVG scatter.

For each industry_group with >= MIN_MAP_COMPANIES shown + embedded companies,
fits scikit-learn PCA(n_components=2, svd_solver="full") over the unit-normalized
description embeddings (migration 0033), projects to 2D, pins a deterministic
sign convention (PCA sign is arbitrary), and min-max normalizes each axis to
[0, 1] WITHIN the cohort, then writes map_x/map_y/map_computed_at (migration
0038). Coords compare only within their industry — the /map/[industry] grain.

scikit-learn lives in the optional ``embeddings`` uv group next to fastembed
(same group compute-themes' KMeans uses); the stage depends only on the
:class:`Projector` Protocol and tests inject a deterministic fake, so scikit-learn
is never required to run them.

Determinism (the idempotence contract): fixed member order (ORDER BY id),
svd_solver="full" (exact SVD, deterministic at any cohort size — unlike "auto",
which switches to the randomized solver above 500 samples), a pinned sign
convention, and deterministic min-max. Unchanged embeddings re-produce
byte-identical coords, so a re-run is a no-op beyond re-stamping map_computed_at.

Sign convention matters BECAUSE of the min-max: negating an axis then min-max
scaling yields the mirror (1 - x), so without a pinned sign two runs could emit
mirror-image maps. For each axis the sample with the largest |score| (ties ->
lowest index) is forced positive.

TTL gate (monthly cadence off weekly discovery.yml), PER INDUSTRY: skip an
industry whose MAX(map_computed_at) is younger than ttl_days (default 25). A new
company joining a fresh industry waits for that industry's next monthly window —
accepted, consistent with the themes "rebuilt as a set" cadence.

$0: local CPU PCA, no LLM, no network.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company

logger = logging.getLogger(__name__)

# Smallest cohort worth a 2D map. PCA(2) needs n>=2, but <5 dots is not a map.
# Below compute-themes' 8 on purpose: a map has no LLM naming, hence no
# coherence floor — it is useful at lower populations. Tunable.
MIN_MAP_COMPANIES: int = 5
DEFAULT_TTL_DAYS: int = 25


class Projector(Protocol):
    """The projection seam: reduce vectors to n_components dimensions.

    The stage depends only on this Protocol; the real scikit-learn adapter
    (:class:`PCAProjector`) is constructed in the CLI, and tests inject a
    deterministic fake so scikit-learn (optional ``embeddings`` group) is never
    required to run them.
    """

    def project(
        self, vectors: list[list[float]], n_components: int
    ) -> list[list[float]]:
        """Return one n_components-length score row per input vector."""
        ...


class PCAProjector:
    """Real adapter over scikit-learn's PCA.

    ``svd_solver="full"``: exact LAPACK SVD, deterministic at any cohort size
    (the idempotence contract). Imports scikit-learn lazily so the module stays
    importable without the optional ``embeddings`` group (lint CI, default
    ``uv sync``).
    """

    def __init__(self) -> None:
        # Import check up front: the CLI constructs this eagerly so a missing
        # optional dependency group fails loudly at startup, not mid-run.
        import sklearn.decomposition  # noqa: F401  # optional dep — see docstring

    def project(
        self, vectors: list[list[float]], n_components: int
    ) -> list[list[float]]:
        from sklearn.decomposition import PCA

        model = PCA(n_components=n_components, svd_solver="full")
        coords = model.fit_transform(vectors)
        return [[float(v) for v in row] for row in coords]


class ComputeMapPositionsSummary(BaseModel):
    """Result of one compute-map-positions run."""

    industries_seen: int = 0  # industries meeting MIN_MAP_COMPANIES
    industries_processed: int = 0  # coords (re)written
    industries_skipped_ttl: int = 0  # fresh — the per-industry gate held
    companies_positioned: int = 0  # rows given map_x/map_y


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a DB or scikit-learn)
# ---------------------------------------------------------------------------


def unit_normalize(vector: list[float]) -> list[float]:
    """Scale to unit length (zero vectors pass through unchanged).

    Cosine-space parity with compute-themes; duplicated here to avoid importing
    that LLM-bound module.
    """
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return list(vector)
    return [x / norm for x in vector]


def _embedding_list(company: Company) -> list[float]:
    """Coerce the pgvector value (numpy array at runtime) to list[float].

    Deliberately ``is not None`` rather than truthiness: an ndarray's __bool__
    raises on >1 element, so ``embedding or []`` would crash.
    """
    emb = company.embedding
    return [float(x) for x in emb] if emb is not None else []


def _pin_sign(scores: list[list[float]]) -> list[list[float]]:
    """Force a deterministic sign per axis: the largest-|value| sample (ties ->
    lowest index) is made positive.

    Essential — min-max encodes sign, so an unpinned axis could emit the mirror
    (1 - x) on a re-run. Operates independently per axis.
    """
    if not scores:
        return scores
    n_axes = len(scores[0])
    flip = [False] * n_axes
    for axis in range(n_axes):
        best_i, best_abs = 0, -1.0
        for i, row in enumerate(scores):
            a = abs(row[axis])
            if a > best_abs:  # strict -> lowest index wins ties
                best_abs, best_i = a, i
        if scores[best_i][axis] < 0.0:
            flip[axis] = True
    return [
        [(-v if flip[axis] else v) for axis, v in enumerate(row)] for row in scores
    ]


def _minmax_axis(values: list[float]) -> list[float]:
    """Min-max to [0, 1]; a constant axis maps to 0.5 (no divide-by-zero)."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    span = hi - lo
    return [(v - lo) / span for v in values]


def finalize_coords(raw_scores: list[list[float]]) -> list[tuple[float, float]]:
    """Sign-pin then per-axis min-max the raw 2D PCA scores to [0, 1]^2.

    Pure + deterministic given ``raw_scores`` — the unit-tested core of the
    stage. Returns [] for an empty input.
    """
    if not raw_scores:
        return []
    signed = _pin_sign(raw_scores)
    xs = _minmax_axis([r[0] for r in signed])
    ys = _minmax_axis([r[1] for r in signed])
    return list(zip(xs, ys, strict=True))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _eligible_industries(session: AsyncSession) -> list[str]:
    """industry_groups with >= MIN_MAP_COMPANIES shown+embedded companies,
    alphabetical for deterministic run order."""
    stmt = (
        select(Company.industry_group)
        .where(
            Company.exclusion_reason.is_(None),
            Company.embedding.is_not(None),
            Company.industry_group.is_not(None),
        )
        .group_by(Company.industry_group)
        .having(func.count(Company.id) >= MIN_MAP_COMPANIES)
        .order_by(Company.industry_group)
    )
    return [row for row in (await session.execute(stmt)).scalars() if row]


async def _fetch_members(session: AsyncSession, industry: str) -> list[Company]:
    """Shown + embedded companies of one industry, id-ordered (deterministic
    input order is part of the PCA reproducibility contract)."""
    stmt = (
        select(Company)
        .where(
            Company.exclusion_reason.is_(None),
            Company.embedding.is_not(None),
            Company.industry_group == industry,
        )
        .order_by(Company.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _industry_fresh(
    session: AsyncSession, industry: str, *, ttl_days: int, now: datetime
) -> bool:
    """True when this industry's coords were computed within the TTL (skip it).

    Reads MAX(map_computed_at) over the same shown+embedded cohort the stage
    positions, so a brand-new industry (all NULL) always recomputes.
    """
    last = (
        await session.execute(
            select(func.max(Company.map_computed_at)).where(
                Company.exclusion_reason.is_(None),
                Company.embedding.is_not(None),
                Company.industry_group == industry,
            )
        )
    ).scalar_one_or_none()
    return last is not None and last >= now - timedelta(days=ttl_days)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run_compute_map_positions(
    session: AsyncSession,
    projector: Projector,
    *,
    ttl_days: int = DEFAULT_TTL_DAYS,
    force: bool = False,
    now: datetime | None = None,
) -> ComputeMapPositionsSummary:
    """Fit + write per-industry 2D map coords. Per-industry incremental commit:
    a crash keeps every industry finished so far, and the TTL gate then holds
    the partial result until the next monthly window."""
    summary = ComputeMapPositionsSummary()
    now = now or datetime.now(UTC)

    industries = await _eligible_industries(session)
    summary.industries_seen = len(industries)

    for industry in industries:
        if not force and await _industry_fresh(
            session, industry, ttl_days=ttl_days, now=now
        ):
            summary.industries_skipped_ttl += 1
            continue

        companies = await _fetch_members(session, industry)
        vectors = [unit_normalize(_embedding_list(c)) for c in companies]
        raw = projector.project(vectors, 2)
        coords = finalize_coords(raw)

        async with session.begin_nested():
            for company, (x, y) in zip(companies, coords, strict=True):
                company.map_x = x
                company.map_y = y
                company.map_computed_at = now
                session.add(company)
        await session.commit()
        summary.industries_processed += 1
        summary.companies_positioned += len(companies)

    logger.info(
        "compute-map-positions: industries=%d/%d skipped_ttl=%d positioned=%d",
        summary.industries_processed,
        summary.industries_seen,
        summary.industries_skipped_ttl,
        summary.companies_positioned,
    )
    return summary
