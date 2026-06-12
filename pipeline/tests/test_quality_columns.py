"""Round-trip test for the catalog-quality columns added in migration 0022."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


@pytest.mark.asyncio
async def test_quality_columns_round_trip(db: AsyncSession) -> None:
    company = Company(
        name="Junk Co",
        slug="junk-co-quality-test",
        normalized_name="junk co quality test",
        hq_country="US",
        exclusion_reason="not_a_startup",
        exclusion_detail="founded 1999; public company",
        excluded_at=datetime.now(tz=UTC),
        eligibility_checked_at=datetime.now(tz=UTC),
        rejected_urls=["https://junk.ai"],
        funding_round_count=2,
    )
    db.add(company)
    await db.commit()

    row = (
        await db.execute(select(Company).where(Company.slug == "junk-co-quality-test"))
    ).scalar_one()
    assert row.exclusion_reason == "not_a_startup"
    assert row.rejected_urls == ["https://junk.ai"]
    assert row.funding_round_count == 2


@pytest.mark.asyncio
async def test_quality_columns_defaults(db: AsyncSession) -> None:
    company = Company(
        name="Default Co",
        slug="default-co-quality-test",
        normalized_name="default co quality test",
        hq_country="US",
    )
    db.add(company)
    await db.commit()
    await db.refresh(company)

    assert company.exclusion_reason is None
    assert company.rejected_urls == []
    assert company.funding_round_count == 0
