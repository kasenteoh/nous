"""Integration tests for the refresh-latest-round pipeline stage (Task C0).

Requires DATABASE_URL (schema applied via ``alembic upgrade head``, which must
include migration 0028). Skipped when DATABASE_URL is unset.

The stage denormalizes each company's most-recent round onto
``companies.latest_round_amount`` / ``latest_round_date`` / ``latest_round_type``
so the web index can sort by funding amount / recency without a cross-table
aggregate. "Most recent" = the round with the greatest ``announced_date``
(NULLS LAST), matching the migration's ``DISTINCT ON`` backfill.

Scenarios:
1. Company with two rounds → latest_round_* reflects the newest-dated round.
2. Company with no rounds → all three columns stay NULL.
3. A round with a NULL announced_date never wins over a dated round.
4. Recompute is set-based and idempotent (a second run is a no-op).
5. A stale latest_round_* (e.g. company's only round was deleted) is reset.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound
from nous.pipeline.refresh_latest_round import refresh_latest_round

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _company(slug: str) -> Company:
    return Company(
        name=slug,
        slug=f"rlr-{slug}",
        normalized_name=f"rlr {slug}",
        hq_country="US",
    )


def _round(
    company: Company,
    *,
    round_type: str | None,
    amount: Decimal | None,
    announced: date | None,
) -> FundingRound:
    return FundingRound(
        company_id=company.id,
        round_type=round_type,
        amount_raised=amount,
        announced_date=announced,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_picks_newest_dated_round(db: AsyncSession) -> None:
    """Latest_round_* reflects the round with the greatest announced_date."""
    company = _company("co-a")
    db.add(company)
    await db.flush()
    db.add_all(
        [
            _round(
                company,
                round_type="Seed",
                amount=Decimal("3000000"),
                announced=date(2022, 1, 1),
            ),
            _round(
                company,
                round_type="Series A",
                amount=Decimal("15000000"),
                announced=date(2024, 6, 1),
            ),
        ]
    )
    await db.commit()

    await refresh_latest_round(db)
    await db.commit()

    await db.refresh(company)
    assert company.latest_round_date == date(2024, 6, 1)
    assert company.latest_round_amount == Decimal("15000000")
    assert company.latest_round_type == "Series A"


async def test_company_with_no_rounds_stays_null(db: AsyncSession) -> None:
    """A company with no funding rounds keeps NULL latest_round_* columns."""
    company = _company("co-b")
    db.add(company)
    await db.commit()

    await refresh_latest_round(db)
    await db.commit()

    await db.refresh(company)
    assert company.latest_round_date is None
    assert company.latest_round_amount is None
    assert company.latest_round_type is None


async def test_null_dated_round_does_not_win(db: AsyncSession) -> None:
    """A round with a NULL announced_date never beats a dated round."""
    company = _company("co-c")
    db.add(company)
    await db.flush()
    db.add_all(
        [
            _round(
                company,
                round_type="Series B",
                amount=Decimal("40000000"),
                announced=date(2023, 3, 1),
            ),
            _round(
                company,
                round_type="Undated",
                amount=Decimal("99000000"),
                announced=None,
            ),
        ]
    )
    await db.commit()

    await refresh_latest_round(db)
    await db.commit()

    await db.refresh(company)
    assert company.latest_round_date == date(2023, 3, 1)
    assert company.latest_round_amount == Decimal("40000000")
    assert company.latest_round_type == "Series B"


async def test_null_dated_round_only(db: AsyncSession) -> None:
    """When a company's only round has a NULL date, it still populates the
    type/amount (date stays NULL)."""
    company = _company("co-c2")
    db.add(company)
    await db.flush()
    db.add(
        _round(
            company,
            round_type="Seed",
            amount=Decimal("500000"),
            announced=None,
        )
    )
    await db.commit()

    await refresh_latest_round(db)
    await db.commit()

    await db.refresh(company)
    assert company.latest_round_date is None
    assert company.latest_round_amount == Decimal("500000")
    assert company.latest_round_type == "Seed"


async def test_is_idempotent(db: AsyncSession) -> None:
    """Running the stage twice produces the same denormalized values."""
    company = _company("co-d")
    db.add(company)
    await db.flush()
    db.add(
        _round(
            company,
            round_type="Series A",
            amount=Decimal("15000000"),
            announced=date(2024, 6, 1),
        )
    )
    await db.commit()

    await refresh_latest_round(db)
    await db.commit()
    await db.refresh(company)
    first = (
        company.latest_round_date,
        company.latest_round_amount,
        company.latest_round_type,
    )

    await refresh_latest_round(db)
    await db.commit()
    await db.refresh(company)
    second = (
        company.latest_round_date,
        company.latest_round_amount,
        company.latest_round_type,
    )

    assert first == second == (date(2024, 6, 1), Decimal("15000000"), "Series A")


async def test_stale_value_is_reset_when_rounds_removed(db: AsyncSession) -> None:
    """A company whose rounds were deleted has its stale latest_round_* cleared."""
    company = _company("co-e")
    db.add(company)
    await db.flush()
    rnd = _round(
        company,
        round_type="Seed",
        amount=Decimal("3000000"),
        announced=date(2022, 1, 1),
    )
    db.add(rnd)
    await db.commit()

    await refresh_latest_round(db)
    await db.commit()
    await db.refresh(company)
    assert company.latest_round_type == "Seed"

    # Remove the round, then recompute — the denormalized fields must reset.
    await db.execute(delete(FundingRound).where(FundingRound.company_id == company.id))
    await db.commit()

    await refresh_latest_round(db)
    await db.commit()
    await db.refresh(company)
    assert company.latest_round_date is None
    assert company.latest_round_amount is None
    assert company.latest_round_type is None
