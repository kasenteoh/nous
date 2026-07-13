"""DB-gated tests for migration 0038 + the compute-map-positions stage.

Covers, against a real Postgres with pgvector (CI: the pgvector/pgvector:pg15
service image; the schema comes from `alembic upgrade head`):

- migration 0038 <-> model: map_x/map_y/map_computed_at round-trip through the
  ORM and are nullable (a company with no coords reads back NULL);
- the full stage flow: an eligible industry positions every member into
  [0, 1]^2 with a stamped map_computed_at;
- the MIN_MAP_COMPANIES floor: a below-threshold industry stays NULL and is
  never even seen;
- the "shown + embedded" filter: excluded and un-embedded companies never get
  coords (they are not members), even in an otherwise-eligible industry;
- the PER-INDUSTRY TTL gate: a freshly-stamped industry is skipped (coords
  untouched) while a stale one recomputes, and --force bypasses the gate;
- idempotence/determinism: unchanged embeddings re-project to byte-identical
  coords across runs — only map_computed_at advances.

The projector is always the deterministic FakeProjector (first-2-dims — no
scikit-learn needed).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline.compute_map_positions import run_compute_map_positions

from .test_compute_map_positions import FakeProjector

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

DIM = 384
NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


def _vec(i: int) -> list[float]:
    """A 384-dim embedding whose first two dims vary per company i.

    The FakeProjector reads dims 0 and 1, so distinct per-company values give a
    real spread of 2D scores (rather than a degenerate all-0.5 axis).
    """
    vec = [0.0] * DIM
    vec[0] = 1.0 + 0.1 * i
    vec[1] = 2.0 - 0.07 * i
    return vec


def _company(slug: str, i: int, **overrides: Any) -> Company:
    defaults: dict[str, Any] = {
        "name": f"Co {slug}",
        "slug": slug,
        "normalized_name": f"co {slug}",
        "description_short": f"Short description for {slug}.",
        "industry_group": "DevTools",
        "embedding": _vec(i),
    }
    defaults.update(overrides)
    return Company(**defaults)


def _seed_industry(
    db: AsyncSession, industry: str, n: int, *, prefix: str, **overrides: Any
) -> list[Company]:
    """n shown + embedded companies in one industry_group."""
    companies = [
        _company(f"{prefix}-{i}", i, industry_group=industry, **overrides)
        for i in range(n)
    ]
    db.add_all(companies)
    return companies


# ---------------------------------------------------------------------------
# Migration 0038 <-> model consistency
# ---------------------------------------------------------------------------


async def test_map_columns_round_trip(db: AsyncSession) -> None:
    positioned = _company("positioned", 0)
    positioned.map_x = 0.25
    positioned.map_y = 0.75
    positioned.map_computed_at = NOW
    unpositioned = _company("unpositioned", 1)  # coords left at their NULL default
    db.add_all([positioned, unpositioned])
    await db.commit()
    db.expire_all()

    got = (
        await db.execute(select(Company).where(Company.slug == "positioned"))
    ).scalar_one()
    assert got.map_x == 0.25
    assert got.map_y == 0.75
    assert got.map_computed_at == NOW

    blank = (
        await db.execute(select(Company).where(Company.slug == "unpositioned"))
    ).scalar_one()
    assert blank.map_x is None
    assert blank.map_y is None
    assert blank.map_computed_at is None


# ---------------------------------------------------------------------------
# Full flow
# ---------------------------------------------------------------------------


async def test_full_run_positions_every_member(db: AsyncSession) -> None:
    _seed_industry(db, "DevTools", 5, prefix="dev")
    await db.commit()

    summary = await run_compute_map_positions(db, FakeProjector(), now=NOW)

    assert summary.industries_seen == 1
    assert summary.industries_processed == 1
    assert summary.industries_skipped_ttl == 0
    assert summary.companies_positioned == 5

    rows = (
        await db.execute(
            select(Company.map_x, Company.map_y, Company.map_computed_at)
        )
    ).all()
    assert len(rows) == 5
    for map_x, map_y, stamp in rows:
        assert map_x is not None and -1e-9 <= map_x <= 1.0 + 1e-9
        assert map_y is not None and -1e-9 <= map_y <= 1.0 + 1e-9
        assert stamp == NOW


# ---------------------------------------------------------------------------
# MIN_MAP_COMPANIES floor
# ---------------------------------------------------------------------------


async def test_below_threshold_industry_stays_null(db: AsyncSession) -> None:
    _seed_industry(db, "Tiny", 4, prefix="tiny")  # 4 < MIN_MAP_COMPANIES (5)
    await db.commit()

    summary = await run_compute_map_positions(db, FakeProjector(), now=NOW)

    assert summary.industries_seen == 0
    assert summary.companies_positioned == 0
    positioned = (
        await db.execute(
            select(func.count())
            .select_from(Company)
            .where(Company.map_x.is_not(None))
        )
    ).scalar_one()
    assert positioned == 0


# ---------------------------------------------------------------------------
# "shown + embedded" membership filter
# ---------------------------------------------------------------------------


async def test_excluded_and_unembedded_are_never_positioned(
    db: AsyncSession,
) -> None:
    _seed_industry(db, "DevTools", 5, prefix="dev")
    # Same industry, but neither is a member: one is excluded, one has no
    # embedding. They must not gain coords, and must not change the count of 5.
    excluded = _company("excluded", 9, exclusion_reason="not_a_startup")
    unembedded = _company("unembedded", 10, embedding=None)
    db.add_all([excluded, unembedded])
    await db.commit()

    summary = await run_compute_map_positions(db, FakeProjector(), now=NOW)

    assert summary.industries_seen == 1
    assert summary.companies_positioned == 5  # the two non-members excluded

    got_excluded = (
        await db.execute(select(Company).where(Company.slug == "excluded"))
    ).scalar_one()
    got_unembedded = (
        await db.execute(select(Company).where(Company.slug == "unembedded"))
    ).scalar_one()
    assert got_excluded.map_x is None
    assert got_unembedded.map_x is None


# ---------------------------------------------------------------------------
# Per-industry TTL gate
# ---------------------------------------------------------------------------


async def test_per_industry_ttl_gate_and_force(db: AsyncSession) -> None:
    # "Fresh": coords stamped at NOW (within the 25-day TTL) -> must be skipped.
    fresh = _seed_industry(db, "Fresh", 5, prefix="fresh")
    for company in fresh:
        company.map_x = 9.0  # sentinel: out of [0, 1] -> proves an overwrite
        company.map_y = 9.0
        company.map_computed_at = NOW
    # "Stale": coords stamped 40 days ago -> older than the TTL, must recompute.
    stale = _seed_industry(db, "Stale", 5, prefix="stale")
    for company in stale:
        company.map_x = 9.0
        company.map_y = 9.0
        company.map_computed_at = NOW - timedelta(days=40)
    await db.commit()

    gated = await run_compute_map_positions(db, FakeProjector(), now=NOW)
    assert gated.industries_seen == 2
    assert gated.industries_skipped_ttl == 1  # Fresh held
    assert gated.industries_processed == 1  # Stale recomputed
    assert gated.companies_positioned == 5

    db.expire_all()
    fresh_row = (
        await db.execute(select(Company).where(Company.slug == "fresh-0"))
    ).scalar_one()
    assert fresh_row.map_x == 9.0  # untouched sentinel
    assert fresh_row.map_computed_at == NOW  # stamp unchanged

    stale_row = (
        await db.execute(select(Company).where(Company.slug == "stale-0"))
    ).scalar_one()
    assert -1e-9 <= stale_row.map_x <= 1.0 + 1e-9  # overwritten into [0, 1]
    assert stale_row.map_computed_at == NOW  # restamped

    # --force bypasses the gate: the Fresh industry now recomputes too.
    later = NOW + timedelta(days=1)
    forced = await run_compute_map_positions(
        db, FakeProjector(), now=later, force=True
    )
    assert forced.industries_skipped_ttl == 0
    assert forced.industries_processed == 2

    db.expire_all()
    fresh_after = (
        await db.execute(select(Company).where(Company.slug == "fresh-0"))
    ).scalar_one()
    assert -1e-9 <= fresh_after.map_x <= 1.0 + 1e-9  # sentinel replaced
    assert fresh_after.map_computed_at == later


# ---------------------------------------------------------------------------
# Idempotence / determinism
# ---------------------------------------------------------------------------


async def test_rerun_is_deterministic(db: AsyncSession) -> None:
    _seed_industry(db, "DevTools", 6, prefix="dev")
    await db.commit()

    await run_compute_map_positions(db, FakeProjector(), now=NOW, force=True)
    db.expire_all()
    first = {
        cid: (mx, my)
        for cid, mx, my in (
            await db.execute(select(Company.id, Company.map_x, Company.map_y))
        ).all()
    }

    later = NOW + timedelta(days=30)
    await run_compute_map_positions(db, FakeProjector(), now=later, force=True)
    db.expire_all()
    rows = (
        await db.execute(
            select(Company.id, Company.map_x, Company.map_y, Company.map_computed_at)
        )
    ).all()

    assert len(rows) == 6
    for cid, mx, my, stamp in rows:
        # Byte-identical coords from unchanged embeddings; only the stamp moves.
        assert (mx, my) == first[cid]
        assert stamp == later
