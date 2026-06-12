"""Round-trip coverage for the M4 competitors table."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Competitor
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


async def test_competitor_row_with_resolved_link(db: AsyncSession) -> None:
    target = _make_company("Acme")
    rival = _make_company("Beta Co")
    db.add_all([target, rival])
    await db.flush()

    row = Competitor(
        company_id=target.id,
        competitor_company_id=rival.id,
        competitor_name="Beta Co",
        description="Direct rival in same market.",
        reasoning="Both target SMB ops teams.",
        rank=1,
    )
    db.add(row)
    await db.flush()

    fetched = await db.get(Competitor, row.id)
    assert fetched is not None
    assert fetched.company_id == target.id
    assert fetched.competitor_company_id == rival.id
    assert fetched.rank == 1


async def test_competitor_row_unlinked(db: AsyncSession) -> None:
    target = _make_company("Acme")
    db.add(target)
    await db.flush()

    row = Competitor(
        company_id=target.id,
        competitor_company_id=None,
        competitor_name="UnknownCo",
        description="Not in our DB.",
        reasoning="Mentioned by the LLM.",
        rank=2,
    )
    db.add(row)
    await db.flush()

    stmt = select(Competitor).where(Competitor.company_id == target.id)
    rows = (await db.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].competitor_company_id is None
    assert rows[0].competitor_name == "UnknownCo"


async def test_unique_company_rank_constraint(db: AsyncSession) -> None:
    target = _make_company("Acme")
    db.add(target)
    await db.flush()

    db.add(
        Competitor(
            company_id=target.id,
            competitor_name="A",
            rank=1,
        )
    )
    await db.flush()

    db.add(
        Competitor(
            company_id=target.id,
            competitor_name="B",
            rank=1,
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_self_reference_rejected(db: AsyncSession) -> None:
    """ck_competitors_no_self_reference forbids a company being its own competitor."""
    target = _make_company("Acme")
    db.add(target)
    await db.flush()

    db.add(
        Competitor(
            company_id=target.id,
            competitor_company_id=target.id,
            competitor_name="Acme",
            rank=1,
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
