"""Round-trip + constraint coverage for the company_investors table."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanyInvestor, Investor
from nous.util.investor_name import canonicalize_investor_name
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(name: str) -> Company:
    return Company(
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{os.urandom(3).hex()}",
        normalized_name=normalize_name(name),
        hq_country="US",
    )


def _make_investor(name: str) -> Investor:
    return Investor(
        name=name,
        name_normalized=canonicalize_investor_name(name),
        slug=f"{name.lower().replace(' ', '-')}-{os.urandom(3).hex()}",
    )


async def test_company_investor_round_trip(db: AsyncSession) -> None:
    company = _make_company("Acme")
    investor = _make_investor(f"Sequoia Capital {os.urandom(3).hex()}")
    db.add_all([company, investor])
    await db.flush()

    link = CompanyInvestor(
        company_id=company.id,
        investor_id=investor.id,
        source="vc_portfolio",
    )
    db.add(link)
    await db.flush()

    fetched = await db.get(CompanyInvestor, link.id)
    assert fetched is not None
    assert fetched.company_id == company.id
    assert fetched.investor_id == investor.id
    assert fetched.source == "vc_portfolio"
    # is_lead defaults to False via the server_default.
    assert fetched.is_lead is False


async def test_company_investor_unique_pair_constraint(db: AsyncSession) -> None:
    company = _make_company("Acme")
    investor = _make_investor(f"Greylock {os.urandom(3).hex()}")
    db.add_all([company, investor])
    await db.flush()

    db.add(
        CompanyInvestor(
            company_id=company.id,
            investor_id=investor.id,
            source="vc_portfolio",
        )
    )
    await db.flush()

    # Same (company, investor) pair — the unique constraint must reject it.
    db.add(
        CompanyInvestor(
            company_id=company.id,
            investor_id=investor.id,
            source="news",
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_company_investor_same_company_different_investors(
    db: AsyncSession,
) -> None:
    """The constraint is on the pair, so one company can have many investors."""
    company = _make_company("Acme")
    inv_a = _make_investor(f"Sequoia {os.urandom(3).hex()}")
    inv_b = _make_investor(f"Greylock {os.urandom(3).hex()}")
    db.add_all([company, inv_a, inv_b])
    await db.flush()

    db.add_all(
        [
            CompanyInvestor(
                company_id=company.id,
                investor_id=inv_a.id,
                source="vc_portfolio",
            ),
            CompanyInvestor(
                company_id=company.id,
                investor_id=inv_b.id,
                source="vc_portfolio",
            ),
        ]
    )
    await db.flush()

    rows = (
        (
            await db.execute(
                select(CompanyInvestor).where(
                    CompanyInvestor.company_id == company.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
