"""Tests for the funding-prominence override helper (nous.util.prominence).

DB-gated (same convention as the other integration suites): ``has_prominent_round``
runs a MAX(amount_raised) SELECT, so the boundary tests need a live Postgres.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound
from nous.util.prominence import (
    PROMINENCE_OVERRIDE_USD,
    has_prominent_round,
    max_recorded_round_usd,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def test_threshold_constant() -> None:
    # A pure guard so an accidental edit to the owner-approved threshold is loud.
    assert PROMINENCE_OVERRIDE_USD == 500_000_000


def _co(slug: str) -> Company:
    return Company(
        name=slug, slug=slug, normalized_name=slug, hq_country="US"
    )


async def _co_with_round(db: AsyncSession, slug: str, amount: Decimal) -> Company:
    co = _co(slug)
    db.add(co)
    await db.flush()
    db.add(FundingRound(company_id=co.id, amount_raised=amount))
    await db.flush()
    return co


async def test_below_threshold_is_not_prominent(db: AsyncSession) -> None:
    co = await _co_with_round(db, "prom-below", Decimal("499999999"))
    assert await has_prominent_round(db, co.id) is False
    assert await max_recorded_round_usd(db, co.id) == Decimal("499999999")


async def test_exactly_threshold_is_prominent(db: AsyncSession) -> None:
    co = await _co_with_round(db, "prom-exact", Decimal("500000000"))
    assert await has_prominent_round(db, co.id) is True


async def test_no_rounds_is_not_prominent(db: AsyncSession) -> None:
    co = _co("prom-none")
    db.add(co)
    await db.flush()
    assert await has_prominent_round(db, co.id) is False
    assert await max_recorded_round_usd(db, co.id) is None


async def test_max_across_multiple_rounds_qualifies(db: AsyncSession) -> None:
    # A small round plus a mega-round: the MAX is what counts.
    co = _co("prom-multi")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            FundingRound(company_id=co.id, amount_raised=Decimal("5000000")),
            FundingRound(company_id=co.id, amount_raised=Decimal("650000000")),
        ]
    )
    await db.flush()
    assert await has_prominent_round(db, co.id) is True
    assert await max_recorded_round_usd(db, co.id) == Decimal("650000000")
