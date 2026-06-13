"""Integration tests for the refresh-investor-counts pipeline stage.

Requires DATABASE_URL (schema applied via ``alembic upgrade head``).
Skipped when DATABASE_URL is unset.

Scenarios tested:
1. Investor linked via company_investors ONLY → counted once.
2. Investor linked via funding_round_investors ONLY → counted once.
3. Investor linked via BOTH tables to the SAME company → counted once (UNION,
   not UNION ALL).
4. Investor linked to companies via BOTH tables (different companies) → count
   equals the number of distinct companies.
5. Excluded company (exclusion_reason IS NOT NULL) is never counted, even when
   linked in both tables.
6. Idempotency: a second run produces the same result.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    CompanyInvestor,
    FundingRound,
    FundingRoundInvestor,
    Investor,
)
from nous.pipeline.refresh_investor_counts import refresh_investor_counts

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _company(slug: str, *, excluded: bool = False) -> Company:
    return Company(
        name=slug,
        slug=f"ric-{slug}",
        normalized_name=f"ric {slug}",
        hq_country="US",
        exclusion_reason="manual" if excluded else None,
    )


def _investor(slug: str) -> Investor:
    return Investor(
        name=f"Firm {slug}",
        name_normalized=f"firm {slug}",
        slug=f"ric-inv-{slug}",
    )


def _ci_link(company: Company, investor: Investor) -> CompanyInvestor:
    return CompanyInvestor(
        company_id=company.id,
        investor_id=investor.id,
        source="vc_portfolio",
    )


def _round(company: Company) -> FundingRound:
    return FundingRound(
        company_id=company.id,
        round_type="Seed",
    )


def _fri_link(funding_round: FundingRound, investor: Investor) -> FundingRoundInvestor:
    return FundingRoundInvestor(
        funding_round_id=funding_round.id,
        investor_id=investor.id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_counts_company_investors_link(db: AsyncSession) -> None:
    """Investor linked via company_investors only → portfolio_count == 1."""
    company = _company("co-a")
    investor = _investor("inv-a")
    db.add_all([company, investor])
    await db.flush()
    db.add(_ci_link(company, investor))
    await db.commit()

    await refresh_investor_counts(db)
    await db.commit()

    await db.refresh(investor)
    assert investor.portfolio_count == 1


async def test_counts_funding_round_investors_link(db: AsyncSession) -> None:
    """Investor linked via funding_round_investors only → portfolio_count == 1."""
    company = _company("co-b")
    investor = _investor("inv-b")
    db.add_all([company, investor])
    await db.flush()
    rnd = _round(company)
    db.add(rnd)
    await db.flush()
    db.add(_fri_link(rnd, investor))
    await db.commit()

    await refresh_investor_counts(db)
    await db.commit()

    await db.refresh(investor)
    assert investor.portfolio_count == 1


async def test_deduplicates_both_link_tables_same_company(db: AsyncSession) -> None:
    """Investor linked via BOTH tables to the SAME company → counted only once."""
    company = _company("co-c")
    investor = _investor("inv-c")
    db.add_all([company, investor])
    await db.flush()
    db.add(_ci_link(company, investor))
    rnd = _round(company)
    db.add(rnd)
    await db.flush()
    db.add(_fri_link(rnd, investor))
    await db.commit()

    await refresh_investor_counts(db)
    await db.commit()

    await db.refresh(investor)
    assert investor.portfolio_count == 1


async def test_counts_distinct_companies_across_both_tables(db: AsyncSession) -> None:
    """Investor backed via both link tables (different companies) → count == 2."""
    company_a = _company("co-d1")
    company_b = _company("co-d2")
    investor = _investor("inv-d")
    db.add_all([company_a, company_b, investor])
    await db.flush()

    # company_a via company_investors
    db.add(_ci_link(company_a, investor))
    # company_b via a funding round
    rnd = _round(company_b)
    db.add(rnd)
    await db.flush()
    db.add(_fri_link(rnd, investor))
    await db.commit()

    await refresh_investor_counts(db)
    await db.commit()

    await db.refresh(investor)
    assert investor.portfolio_count == 2


async def test_excluded_company_not_counted(db: AsyncSession) -> None:
    """Excluded company (exclusion_reason IS NOT NULL) is never counted.

    An investor linked to one non-excluded company AND one excluded company
    should have portfolio_count == 1, not 2.
    """
    company_ok = _company("co-e-ok")
    company_ex = _company("co-e-ex", excluded=True)
    investor = _investor("inv-e")
    db.add_all([company_ok, company_ex, investor])
    await db.flush()

    # Non-excluded via company_investors
    db.add(_ci_link(company_ok, investor))
    # Excluded via company_investors AND a funding round — both should be
    # ignored regardless of which link table surfaces them.
    db.add(_ci_link(company_ex, investor))
    rnd = _round(company_ex)
    db.add(rnd)
    await db.flush()
    db.add(_fri_link(rnd, investor))
    await db.commit()

    await refresh_investor_counts(db)
    await db.commit()

    await db.refresh(investor)
    assert investor.portfolio_count == 1


async def test_investor_with_no_links_stays_zero(db: AsyncSession) -> None:
    """Investor with no company links at all → portfolio_count == 0."""
    investor = _investor("inv-f")
    db.add(investor)
    await db.commit()

    await refresh_investor_counts(db)
    await db.commit()

    await db.refresh(investor)
    assert investor.portfolio_count == 0


async def test_is_idempotent(db: AsyncSession) -> None:
    """Running the stage twice produces the same result."""
    company = _company("co-g")
    investor = _investor("inv-g")
    db.add_all([company, investor])
    await db.flush()
    db.add(_ci_link(company, investor))
    await db.commit()

    await refresh_investor_counts(db)
    await db.commit()
    await db.refresh(investor)
    count_after_first = investor.portfolio_count

    await refresh_investor_counts(db)
    await db.commit()
    await db.refresh(investor)
    count_after_second = investor.portfolio_count

    assert count_after_first == count_after_second == 1


async def test_previously_nonzero_count_is_reset_when_company_excluded(
    db: AsyncSession,
) -> None:
    """A re-run after a company becomes excluded zeros the stale count.

    Simulates the scenario where an investor had portfolio_count=1 from a
    prior run, but the company was subsequently excluded. The recompute must
    set the count back to 0, not leave the stale 1.
    """
    company = _company("co-h")
    investor = _investor("inv-h")
    db.add_all([company, investor])
    await db.flush()
    db.add(_ci_link(company, investor))
    # Manually set an incorrect stale count to prove the reset path.
    investor.portfolio_count = 1
    await db.commit()

    # Now exclude the company (as a judge-eligibility or manual exclusion would).
    company.exclusion_reason = "not_a_startup"
    db.add(company)
    await db.commit()

    await refresh_investor_counts(db)
    await db.commit()

    await db.refresh(investor)
    assert investor.portfolio_count == 0
