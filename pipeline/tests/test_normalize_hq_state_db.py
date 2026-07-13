"""DB-gated integration tests for the normalize-hq-state stage.

Requires DATABASE_URL pointing at a Postgres with the schema at head (same
gating as the other stage suites). Exercises ``run_normalize_hq_state`` over real
rows: full names and odd-case codes are rewritten to the canonical USPS code,
already-canonical / non-US / garbage rows are left untouched, --limit and
--dry-run behave, and a second run is idempotent. The pure ``canonical_us_state``
helper is unit-tested (no DB) in test_us_state.py.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline.normalize_hq_state import run_normalize_hq_state
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(name: str, hq_state: str | None) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{normalize_name(name) or 'company'}-{suffix}",
        normalized_name=normalize_name(name),
        website=f"https://{suffix}.example.com",
        hq_country="US",
        hq_state=hq_state,
    )


async def test_full_name_and_case_variants_normalized(db: AsyncSession) -> None:
    """Full names and odd-case codes collapse to the canonical USPS code."""
    full = _make_company("Full Name Co", "California")
    lower = _make_company("Lower Code Co", "ca")
    spaced = _make_company("Spaced Code Co", "NY ")
    db.add_all([full, lower, spaced])
    await db.commit()
    ids = (full.id, lower.id, spaced.id)

    summary = await run_normalize_hq_state(db)
    assert summary.normalized == 3

    for co_id, expected in zip(ids, ("CA", "CA", "NY"), strict=True):
        refetched = await db.get(Company, co_id)
        assert refetched is not None
        assert refetched.hq_state == expected


async def test_canonical_and_non_us_untouched(db: AsyncSession) -> None:
    """Already-canonical codes, foreign regions, and garbage are not selected."""
    canonical = _make_company("Canonical Co", "TX")
    foreign = _make_company("Foreign Co", "Ontario")
    garbage = _make_company("Garbage Co", "San Francisco")
    territory = _make_company("Territory Co", "Puerto Rico")  # out of 50+DC scope
    db.add_all([canonical, foreign, garbage, territory])
    await db.commit()
    ids_expected = {
        canonical.id: "TX",
        foreign.id: "Ontario",
        garbage.id: "San Francisco",
        territory.id: "Puerto Rico",
    }

    summary = await run_normalize_hq_state(db)
    # None of these four should be rewritten. (companies_seen may be >0 if other
    # rows in the shared DB need work, so assert on our rows, not the count.)
    for co_id, expected in ids_expected.items():
        refetched = await db.get(Company, co_id)
        assert refetched is not None
        assert refetched.hq_state == expected
    assert summary is not None


async def test_dry_run_writes_nothing(db: AsyncSession) -> None:
    co = _make_company("Dry Run Co", "California")
    db.add(co)
    await db.commit()
    co_id = co.id

    summary = await run_normalize_hq_state(db, dry_run=True)
    assert summary.normalized >= 1  # it WOULD rewrite ours

    refetched = await db.get(Company, co_id)
    assert refetched is not None
    assert refetched.hq_state == "California"  # but did not


async def test_limit_bounds_work(db: AsyncSession) -> None:
    a = _make_company("Limit A Co", "California")
    b = _make_company("Limit B Co", "texas")
    db.add_all([a, b])
    await db.commit()

    summary = await run_normalize_hq_state(db, limit=1)
    assert summary.companies_seen == 1
    assert summary.normalized == 1


async def test_second_run_is_idempotent(db: AsyncSession) -> None:
    co = _make_company("Idempotent Co", "california")
    db.add(co)
    await db.commit()
    co_id = co.id

    first = await run_normalize_hq_state(db)
    assert first.normalized >= 1

    refetched = await db.get(Company, co_id)
    assert refetched is not None
    assert refetched.hq_state == "CA"

    # A second pass over the same row finds nothing to change for it. Assert the
    # row is stable (other shared-DB rows are irrelevant to this row's fixpoint).
    await run_normalize_hq_state(db)
    refetched2 = await db.get(Company, co_id)
    assert refetched2 is not None
    assert refetched2.hq_state == "CA"
