"""DB-gated tests for migration 0036 — funding_by_quarter +
industry_funding_momentum (industry pages / trends).

Against a real Postgres (CI: pgvector/pgvector:pg15; schema from
`alembic upgrade head`). The RPCs key on SQL CURRENT_DATE, so the seed dates
are computed RELATIVE to now via compute_themes.funding_windows — the same
window math the momentum RPC reproduces — which keeps the assertions stable
whenever the suite runs. The `db` fixture wraps each test in a rolled-back
outer transaction, so a fresh DB sees only this test's rows.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound
from nous.pipeline.compute_themes import funding_windows

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(slug: str, **overrides: Any) -> Company:
    defaults: dict[str, Any] = {
        "name": f"Co {slug}",
        "slug": slug,
        "normalized_name": f"co {slug}",
        "description_short": f"Short description for {slug}.",  # passes catalog bar
    }
    defaults.update(overrides)
    return Company(**defaults)


async def test_industry_momentum_windows_and_catalog_bar(db: AsyncSession) -> None:
    """recent = last 2 complete quarters, prior = the 2 before; the in-progress
    quarter and excluded companies are both left out."""
    prior_start, recent_start, recent_end = funding_windows(date.today())
    industry = "TestMomentumIndustry"

    shown = _make_company("mom-shown", industry_group=industry)
    excluded = _make_company(
        "mom-excluded", industry_group=industry, exclusion_reason="manual"
    )
    db.add_all([shown, excluded])
    await db.flush()
    db.add_all(
        [
            FundingRound(
                company_id=shown.id,
                announced_date=recent_start + timedelta(days=10),
                amount_raised=Decimal("10000000"),  # recent
            ),
            FundingRound(
                company_id=shown.id,
                announced_date=prior_start + timedelta(days=10),
                amount_raised=Decimal("4000000"),  # prior
            ),
            FundingRound(
                company_id=shown.id,
                announced_date=recent_end + timedelta(days=10),
                amount_raised=Decimal("99000000"),  # in-progress → excluded
            ),
            FundingRound(
                company_id=excluded.id,
                announced_date=recent_start + timedelta(days=5),
                amount_raised=Decimal("50000000"),  # excluded company → ignored
            ),
        ]
    )
    await db.commit()

    rows = (
        await db.execute(
            text(
                "SELECT industry_group, recent_usd, prior_usd, round_count "
                "FROM industry_funding_momentum()"
            )
        )
    ).all()
    by_industry = {r.industry_group: r for r in rows}

    assert industry in by_industry
    row = by_industry[industry]
    assert row.recent_usd == Decimal("10000000")
    assert row.prior_usd == Decimal("4000000")
    assert row.round_count == 1  # only the one shown-company recent round


async def test_funding_by_quarter_sums_and_industry_filter(db: AsyncSession) -> None:
    prior_start, recent_start, _recent_end = funding_windows(date.today())
    industry = "TestQuarterIndustry"

    shown = _make_company("fbq-shown", industry_group=industry)
    db.add(shown)
    await db.flush()
    db.add_all(
        [
            FundingRound(
                company_id=shown.id,
                announced_date=recent_start + timedelta(days=10),
                amount_raised=Decimal("7000000"),
            ),
            FundingRound(
                company_id=shown.id,
                announced_date=prior_start + timedelta(days=10),
                amount_raised=Decimal("3000000"),
            ),
        ]
    )
    await db.commit()

    scoped = (
        await db.execute(
            text(
                "SELECT quarter_start, total_usd, round_count "
                "FROM funding_by_quarter(8, :g) ORDER BY quarter_start"
            ),
            {"g": industry},
        )
    ).all()
    assert sum(r.total_usd for r in scoped) == Decimal("10000000")
    assert sum(r.round_count for r in scoped) == 2

    # A non-matching industry filter yields nothing.
    other = (
        await db.execute(
            text("SELECT count(*) FROM funding_by_quarter(8, 'NoSuchIndustryXYZ')")
        )
    ).scalar_one()
    assert other == 0
