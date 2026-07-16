"""compute-momentum: the weekly "heating up" score for every shown company.

Scores each shown company's momentum into ``companies.momentum_score`` (+ a
freshness stamp and pre-worded "why" chips, migration 0039) as a
weight-renormalized mean over the PRESENT of three components, each mapped to
[0, 1] where 0.5 = flat and >0.5 = accelerating:

    A. News acceleration (weight 0.50) — company_snapshots.news_count_30d
       averaged over the last ~2 weeks (recent) vs weeks 3–9 back (baseline).
       news_count_30d is a trailing-30-day rolling count, so two snapshots a
       month apart cover near-disjoint windows: their ratio is a clean
       recent-month-vs-prior-month news measure. +K Laplace smoothing stops a
       0→3 read as ∞; a [1/CAP, CAP] clip caps a one-off blowup; the 2-week
       recent mean dampens a single anomalous week.
    B. Funding recency (weight 0.35) — exp-decay on the denormalized
       companies.latest_round_date (no join). Works with ZERO snapshot history,
       so a just-raised cold-start company still scores.
    C. Headcount growth (weight 0.15) — snapshot employee-midpoint growth over a
       quarter+ window. Low weight and long window on purpose: estimate-employees
       has a 90-day back-off, so weekly headcount snapshots are near-constant and
       frequently NULL. Effectively dormant until enough spaced readings exist.

Missing components DROP OUT (weights re-normalize over the present) rather than
drag the score down; all-absent → NULL (never fabricated). Everything is
anchored to a single ``as_of_week`` (the current ISO-week Monday, or the
``--as-of-week`` override normalized to its Monday) — recency decay and window
edges are computed against it, not wall-clock — so a same-week re-run is
byte-identical in ``momentum_score`` (only ``momentum_computed_at`` re-stamps).

$0, fully local: one batched snapshot read + batched UPDATEs, pure arithmetic,
no LLM / network / scikit-learn.

A company that EXITS the shown cohort (loses both description and funding, or
becomes excluded) has its momentum columns cleared back to NULL at the end of
each run — matching compute_completeness's exit-cohort clear — so only
currently-shown companies ever carry a score and no read path needs a
staleness caveat.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import ColumnElement, CursorResult, and_, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanySnapshot
from nous.pipeline.snapshot_companies import iso_week_monday

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants (mirror the MIN_MAP_COMPANIES / NEWS_WINDOW_DAYS style —
# named, documented, tunable in one place).
# ---------------------------------------------------------------------------

# Component weights. The score is Σ w·norm / Σ w over the PRESENT components, so
# these are relative — a funding-only company scores purely on B. News leads:
# it is the truest weekly signal; headcount trails (slow + sparse).
W_NEWS: float = 0.50
W_FUNDING: float = 0.35
W_HEADCOUNT: float = 0.15
COMPONENT_WEIGHTS: dict[str, float] = {
    "news": W_NEWS,
    "funding": W_FUNDING,
    "headcount": W_HEADCOUNT,
}

# News acceleration. Laplace +K keeps 0→3 finite; the ratio is clipped to
# [1/CAP, CAP] so a spike saturates rather than dominates.
NEWS_SMOOTH_K: float = 3.0
NEWS_RATIO_CAP: float = 4.0
# "recent" = snapshots strictly within the last NEWS_RECENT_WEEKS ISO weeks of
# as_of (the 2 most recent weekly Mondays); "baseline" = snapshots whose
# captured_week is NEWS_BASELINE_MIN_DAYS..NEWS_BASELINE_MAX_DAYS before as_of
# (weeks ~3–9 back). Week 2 is a deliberate buffer so the two 30-day windows are
# more disjoint.
NEWS_RECENT_WEEKS: int = 2
NEWS_BASELINE_MIN_DAYS: int = 21
NEWS_BASELINE_MAX_DAYS: int = 63
# Above this recent-vs-baseline ratio the "why" chip reports the % acceleration
# ("news +180%"); below it, the recent monthly volume ("5 news mentions").
NEWS_WHY_ACCEL_RATIO: float = 1.5

# Funding recency exp-decay time constant (days). today→1.0, 180d→~0.37,
# 1y→~0.13. A future date (data error) clamps to 1.0.
FUNDING_DECAY_TAU_DAYS: float = 180.0
# "raised Nwks ago" below this many days since the last raise; "raised Nmo ago"
# at or above it (kept in the "why" chip only, not the score).
FUNDING_WHY_MONTHS_CUTOFF_DAYS: int = 90

# Headcount growth needs a recent non-null snapshot AND an older one at least
# this many days before it (a quarter+), because weekly headcount barely moves.
HEADCOUNT_MIN_GAP_DAYS: int = 56

# How far back to load snapshots — comfortably spans the 63-day news baseline
# and gives the headcount baseline its 56-day gap. Capped at as_of so a backfill
# (--as-of-week in the past) never reads a future snapshot.
SNAPSHOT_LOOKBACK_DAYS: int = 70

# Companies per commit. Batched begin_nested + commit so a crash keeps every
# finished batch (mirrors compute-map-positions' per-industry commit).
MOMENTUM_BATCH_SIZE: int = 500


class ComputeMomentumSummary(BaseModel):
    """Result of one compute-momentum run."""

    as_of_week: date
    companies_seen: int = 0  # shown companies processed
    companies_scored: int = 0  # non-NULL momentum_score written
    companies_null_low_confidence: int = 0  # NULL score (no present component)
    companies_cleared: int = 0  # exited-cohort companies reset to NULL


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a DB)
# ---------------------------------------------------------------------------


def _mean(values: list[int]) -> float | None:
    """Arithmetic mean, or None for an empty list (the sparse-history guard)."""
    return sum(values) / len(values) if values else None


def news_ratio(recent_counts: list[int], baseline_counts: list[int]) -> float | None:
    """Smoothed, clipped recent-vs-baseline news ratio, or None if either window
    is empty (insufficient snapshot history — Component A drops out, ABSENT).

    ``(recent + K) / (baseline + K)`` clipped to ``[1/CAP, CAP]``: the +K stops
    a 0→N read from exploding to ∞; the clip caps a one-off blowup.
    """
    recent = _mean(recent_counts)
    baseline = _mean(baseline_counts)
    if recent is None or baseline is None:
        return None
    raw = (recent + NEWS_SMOOTH_K) / (baseline + NEWS_SMOOTH_K)
    return min(max(raw, 1.0 / NEWS_RATIO_CAP), NEWS_RATIO_CAP)


def news_norm_from_ratio(ratio: float) -> float:
    """Map a [1/CAP, CAP] ratio to [0, 1] on a log scale: flat(1.0)→0.5,
    CAP→1.0, 1/CAP→0.0. Log so a 2× and a ½× are symmetric about 0.5."""
    return (math.log(ratio) + math.log(NEWS_RATIO_CAP)) / (2.0 * math.log(NEWS_RATIO_CAP))


def news_component(
    recent_counts: list[int], baseline_counts: list[int]
) -> float | None:
    """Component A norm in [0, 1] (0.5 = flat), or None when ABSENT."""
    ratio = news_ratio(recent_counts, baseline_counts)
    return None if ratio is None else news_norm_from_ratio(ratio)


def funding_days_since(latest_round_date: date | None, as_of: date) -> int | None:
    """Days from the latest round to ``as_of`` (never negative), or None when no
    round date is known (Component B ABSENT — an unfunded but news-hot company
    still scores on A alone)."""
    if latest_round_date is None:
        return None
    return max((as_of - latest_round_date).days, 0)


def funding_component(latest_round_date: date | None, as_of: date) -> float | None:
    """Component B norm in (0, 1]: exp-decay on recency, or None when ABSENT."""
    days = funding_days_since(latest_round_date, as_of)
    if days is None:
        return None
    return math.exp(-days / FUNDING_DECAY_TAU_DAYS)


def headcount_growth(
    recent_mid: float | None, baseline_mid: float | None
) -> float | None:
    """Fractional headcount growth ``(recent − baseline) / baseline``, or None
    when either reading is missing or the baseline is 0 (growth undefined over a
    zero base — Component C ABSENT rather than a divide-by-zero)."""
    if recent_mid is None or baseline_mid is None or baseline_mid == 0:
        return None
    return (recent_mid - baseline_mid) / baseline_mid


def headcount_component(
    recent_mid: float | None, baseline_mid: float | None
) -> float | None:
    """Component C norm in [0, 1]: 0.5 = flat, doubling → 1.0, or None when
    ABSENT. ``clip(0.5 + growth/2, 0, 1)``."""
    growth = headcount_growth(recent_mid, baseline_mid)
    if growth is None:
        return None
    return min(max(0.5 + growth / 2.0, 0.0), 1.0)


def combine(components: dict[str, float | None]) -> tuple[float | None, float]:
    """Fold present component norms into ``(score, confidence)``.

    ``score`` = Σ w·norm / Σ w over the PRESENT components (weights re-normalized
    so a missing signal drops out rather than drags the mean); None when nothing
    is present (never fabricated). ``confidence`` = present-weight / total-weight
    ∈ [0, 1] — informational, not persisted (the web gates the badge on the
    score alone, not confidence).
    """
    present = {name: norm for name, norm in components.items() if norm is not None}
    if not present:
        return None, 0.0
    present_weight = sum(COMPONENT_WEIGHTS[name] for name in present)
    score = (
        sum(COMPONENT_WEIGHTS[name] * norm for name, norm in present.items())
        / present_weight
    )
    confidence = present_weight / sum(COMPONENT_WEIGHTS.values())
    return score, confidence


def build_why(
    *,
    news_recent: float | None,
    news_ratio_value: float | None,
    funding_days: int | None,
    headcount_growth_value: float | None,
) -> list[str]:
    """Pre-worded display chips for the present components, strongest-weight
    first (news, funding, headcount). The web joins these with " · " verbatim.

    Empty for an all-absent (NULL-score) company. Deterministic given its inputs.
    """
    why: list[str] = []
    # A — news (0.50): show acceleration when pronounced, else recent volume.
    if news_recent is not None and news_ratio_value is not None:
        if news_ratio_value >= NEWS_WHY_ACCEL_RATIO:
            why.append(f"news +{round((news_ratio_value - 1.0) * 100)}%")
        else:
            why.append(f"{round(news_recent)} news mentions")
    # B — funding (0.35): recency of the latest raise.
    if funding_days is not None:
        if funding_days < 7:
            why.append("raised this week")
        elif funding_days < FUNDING_WHY_MONTHS_CUTOFF_DAYS:
            weeks = round(funding_days / 7)
            why.append(f"raised {weeks}wk{'s' if weeks != 1 else ''} ago")
        else:
            why.append(f"raised {round(funding_days / 30)}mo ago")
    # C — headcount (0.15): team growth over the window (only if it moved).
    if headcount_growth_value is not None:
        pct = round(headcount_growth_value * 100)
        if pct > 0:
            why.append(f"+{pct}% team")
        elif pct < 0:
            why.append(f"{pct}% team")
    return why


def _midpoint(lo: int | None, hi: int | None) -> float | None:
    """Midpoint of a headcount range; the present bound if only one is set;
    None if neither (that snapshot carries no headcount signal)."""
    if lo is not None and hi is not None:
        return (lo + hi) / 2.0
    if lo is not None:
        return float(lo)
    if hi is not None:
        return float(hi)
    return None


# ---------------------------------------------------------------------------
# Per-company scoring (pure over a company's snapshot series + funding date)
# ---------------------------------------------------------------------------


class _CompanyMomentum(BaseModel):
    """The write payload + "why" inputs for one company (internal)."""

    score: float | None
    why: list[str]


def _news_windows(
    snapshots: list[CompanySnapshot], as_of: date
) -> tuple[list[int], list[int]]:
    """(recent_counts, baseline_counts) news_count_30d lists for one company.

    recent = the last NEWS_RECENT_WEEKS ISO weeks (strictly, so the boundary
    week 2 is excluded); baseline = weeks whose captured_week is
    NEWS_BASELINE_MIN_DAYS..NEWS_BASELINE_MAX_DAYS before as_of.
    """
    recent_floor = as_of - timedelta(days=NEWS_RECENT_WEEKS * 7)
    baseline_lo = as_of - timedelta(days=NEWS_BASELINE_MAX_DAYS)
    baseline_hi = as_of - timedelta(days=NEWS_BASELINE_MIN_DAYS)
    recent: list[int] = []
    baseline: list[int] = []
    for snap in snapshots:
        cw = snap.captured_week
        if cw > recent_floor and cw <= as_of:
            recent.append(snap.news_count_30d)
        elif baseline_lo <= cw <= baseline_hi:
            baseline.append(snap.news_count_30d)
    return recent, baseline


def _headcount_midpoints(
    snapshots: list[CompanySnapshot],
) -> tuple[float | None, float | None]:
    """(recent_mid, baseline_mid) for one company: the most-recent non-null
    headcount midpoint, and the OLDEST non-null midpoint at least
    HEADCOUNT_MIN_GAP_DAYS before it. Either None → Component C ABSENT."""
    dated = [
        (snap.captured_week, mid)
        for snap in snapshots
        if (mid := _midpoint(snap.employee_count_min, snap.employee_count_max))
        is not None
    ]
    if not dated:
        return None, None
    recent_week, recent_mid = max(dated, key=lambda pair: pair[0])
    gap_cutoff = recent_week - timedelta(days=HEADCOUNT_MIN_GAP_DAYS)
    older = [(cw, mid) for cw, mid in dated if cw <= gap_cutoff]
    if not older:
        return recent_mid, None
    _, baseline_mid = min(older, key=lambda pair: pair[0])
    return recent_mid, baseline_mid


def score_company(
    *,
    snapshots: list[CompanySnapshot],
    latest_round_date: date | None,
    as_of: date,
) -> _CompanyMomentum:
    """Score one company from its snapshot series + denormalized funding date.

    Pure and deterministic given (snapshots, latest_round_date, as_of): the
    idempotence contract. Returns the score (or None) and its "why" chips.
    """
    recent_counts, baseline_counts = _news_windows(snapshots, as_of)
    recent_mid, baseline_mid = _headcount_midpoints(snapshots)

    news_norm = news_component(recent_counts, baseline_counts)
    funding_norm = funding_component(latest_round_date, as_of)
    hc_norm = headcount_component(recent_mid, baseline_mid)

    score, confidence = combine(
        {"news": news_norm, "funding": funding_norm, "headcount": hc_norm}
    )

    why = build_why(
        news_recent=_mean(recent_counts),
        news_ratio_value=news_ratio(recent_counts, baseline_counts),
        funding_days=funding_days_since(latest_round_date, as_of),
        headcount_growth_value=headcount_growth(recent_mid, baseline_mid),
    )
    logger.debug(
        "momentum: news=%s funding=%s headcount=%s -> score=%s confidence=%.2f",
        news_norm,
        funding_norm,
        hc_norm,
        score,
        confidence,
    )
    return _CompanyMomentum(score=score, why=why)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


# The "shown" cohort predicate — not soft-excluded AND has a short description
# or ≥1 funding round. Defined once so the scoring SELECT and the clear-stale
# UPDATE (its exact negation) can never drift (mirrors
# compute_completeness._shown_predicate; kept module-local per the codebase
# idiom — each stage defines its cohort inline).
def _shown_predicate() -> ColumnElement[bool]:
    return and_(
        Company.exclusion_reason.is_(None),
        or_(
            Company.description_short.is_not(None),
            Company.funding_round_count > 0,
        ),
    )


async def _shown_companies(session: AsyncSession) -> list[Company]:
    """The catalog "shown" cohort, id-ordered (deterministic run order).

    Mirrors the web catalog bar / the other stages' selection: not soft-excluded
    AND has either a short description or at least one funding round.
    """
    stmt = select(Company).where(_shown_predicate()).order_by(Company.id)
    return list((await session.execute(stmt)).scalars().all())


async def _snapshots_by_company(
    session: AsyncSession, *, as_of: date
) -> dict[UUID, list[CompanySnapshot]]:
    """All snapshots in [as_of − SNAPSHOT_LOOKBACK_DAYS, as_of], grouped by
    company_id. One round-trip; the ≤10-weeks-per-company window keeps it small.
    Capped at as_of so a past-week backfill never sees future snapshots."""
    window_start = as_of - timedelta(days=SNAPSHOT_LOOKBACK_DAYS)
    stmt = (
        select(CompanySnapshot)
        .where(
            CompanySnapshot.captured_week >= window_start,
            CompanySnapshot.captured_week <= as_of,
        )
        .order_by(CompanySnapshot.company_id, CompanySnapshot.captured_week.desc())
    )
    grouped: dict[UUID, list[CompanySnapshot]] = defaultdict(list)
    for snap in (await session.execute(stmt)).scalars():
        grouped[snap.company_id].append(snap)
    return grouped


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run_compute_momentum(
    session: AsyncSession,
    *,
    as_of_week: date | None = None,
    now: datetime | None = None,
) -> ComputeMomentumSummary:
    """(Re)score every shown company's weekly momentum, writing momentum_score,
    momentum_computed_at, and momentum_why for ALL of them (value OR NULL) so a
    company that loses its signal drops to NULL rather than keeping a stale
    score. Batched begin_nested commits: a crash keeps every finished batch.

    ``as_of_week`` (any date; normalized to its ISO-week Monday) anchors the
    windows/decay for determinism; defaults to the current ISO week. ``now``
    (defaults to wall-clock UTC) is the momentum_computed_at stamp.
    """
    as_of = iso_week_monday(as_of_week if as_of_week is not None else date.today())
    now = now or datetime.now(UTC)
    summary = ComputeMomentumSummary(as_of_week=as_of)

    companies = await _shown_companies(session)
    summary.companies_seen = len(companies)
    grouped = await _snapshots_by_company(session, as_of=as_of)

    for start in range(0, len(companies), MOMENTUM_BATCH_SIZE):
        batch = companies[start : start + MOMENTUM_BATCH_SIZE]
        async with session.begin_nested():
            for company in batch:
                result = score_company(
                    snapshots=grouped.get(company.id, []),
                    latest_round_date=company.latest_round_date,
                    as_of=as_of,
                )
                company.momentum_score = result.score
                company.momentum_why = result.why
                company.momentum_computed_at = now
                session.add(company)
                if result.score is None:
                    summary.companies_null_low_confidence += 1
                else:
                    summary.companies_scored += 1
        await session.commit()

    # Clear momentum for companies that have EXITED the shown cohort since they
    # were last scored (lost both description and funding, or became excluded),
    # so the stored columns stay self-consistent — only currently-shown companies
    # carry a score. Today every momentum read path re-applies the shown filter,
    # so this closes latent (not live) staleness: a future read path, an export,
    # or the data-quality report can trust the column without re-deriving the
    # cohort. The WHERE is the exact negation of the scoring SELECT's predicate,
    # so no currently-shown company is ever cleared. synchronize_session=False:
    # the cleared rows are disjoint from the shown ORM objects just scored.
    # (Mirrors compute_completeness's exit-cohort clear, which pioneered this —
    # its docstring's "deliberate divergence from compute_momentum" note is
    # retired by this change.)
    cleared = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Company)
            .where(
                Company.momentum_computed_at.is_not(None),
                not_(_shown_predicate()),
            )
            .values(
                momentum_score=None,
                momentum_why=None,
                momentum_computed_at=None,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    summary.companies_cleared = cleared.rowcount or 0
    await session.commit()

    logger.info(
        "compute-momentum: as_of_week=%s seen=%d scored=%d null=%d cleared=%d",
        as_of.isoformat(),
        summary.companies_seen,
        summary.companies_scored,
        summary.companies_null_low_confidence,
        summary.companies_cleared,
    )
    return summary
