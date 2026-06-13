"""Tests for the CompanyRelationship model + its merge_companies handling.

Requires DATABASE_URL (with the schema applied via ``alembic upgrade head``).
Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanyRelationship
from nous.db.upsert import merge_companies

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _company(name: str, slug: str) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
    )


def _edge(
    company_id: object,
    related_company_id: object,
    *,
    rel_type: str = "similar",
    score: Decimal | int = 2,
    source: str = "industry_tags",
) -> CompanyRelationship:
    return CompanyRelationship(
        company_id=company_id,  # type: ignore[arg-type]
        related_company_id=related_company_id,  # type: ignore[arg-type]
        relationship_type=rel_type,
        score=Decimal(score),
        source=source,
    )


async def test_edge_persists(db: AsyncSession) -> None:
    a, b = _company("Edge A", "rel-edge-a"), _company("Edge B", "rel-edge-b")
    db.add_all([a, b])
    await db.flush()
    db.add(_edge(a.id, b.id))
    await db.commit()

    rows = (
        (
            await db.execute(
                select(CompanyRelationship).where(
                    CompanyRelationship.company_id == a.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].related_company_id == b.id
    assert rows[0].relationship_type == "similar"


async def test_duplicate_triple_rejected(db: AsyncSession) -> None:
    a, b = _company("Dup A", "rel-dup-a"), _company("Dup B", "rel-dup-b")
    db.add_all([a, b])
    await db.flush()
    db.add(_edge(a.id, b.id, rel_type="similar"))
    await db.commit()

    # Same (company_id, related_company_id, relationship_type) — must be rejected.
    db.add(_edge(a.id, b.id, rel_type="similar", score=9))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_same_pair_different_type_allowed(db: AsyncSession) -> None:
    """The unique key includes relationship_type, so a 'competitor' and a
    'similar' edge between the same pair coexist."""
    a, b = _company("Pair A", "rel-pair-a"), _company("Pair B", "rel-pair-b")
    db.add_all([a, b])
    await db.flush()
    db.add(_edge(a.id, b.id, rel_type="similar"))
    db.add(_edge(a.id, b.id, rel_type="competitor", source="competitors"))
    await db.commit()

    rows = (
        (
            await db.execute(
                select(CompanyRelationship).where(
                    CompanyRelationship.company_id == a.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.relationship_type for r in rows} == {"similar", "competitor"}


async def test_self_edge_rejected(db: AsyncSession) -> None:
    a = _company("Self A", "rel-self-a")
    db.add(a)
    await db.flush()
    db.add(_edge(a.id, a.id))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_invalid_type_rejected(db: AsyncSession) -> None:
    a, b = _company("Type A", "rel-type-a"), _company("Type B", "rel-type-b")
    db.add_all([a, b])
    await db.flush()
    db.add(_edge(a.id, b.id, rel_type="frenemy"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_merge_drops_edges_touching_loser(db: AsyncSession) -> None:
    """merge_companies removes every edge touching the loser (either direction)
    and leaves the survivor's edges to other companies intact."""
    surv = _company("Surv Inc", "rel-merge-surv")
    loser = _company("Loser Inc", "rel-merge-loser")
    other = _company("Other Inc", "rel-merge-other")
    db.add_all([surv, loser, other])
    await db.flush()

    db.add_all(
        [
            _edge(surv.id, other.id, rel_type="similar"),  # keep
            _edge(loser.id, other.id, rel_type="similar"),  # drop (company=loser)
            _edge(other.id, loser.id, rel_type="similar"),  # drop (related=loser)
            _edge(surv.id, loser.id, rel_type="competitor", source="competitors"),  # drop
            _edge(loser.id, surv.id, rel_type="competitor", source="competitors"),  # drop
        ]
    )
    await db.commit()

    await merge_companies(db, survivor_id=surv.id, loser_id=loser.id)
    await db.commit()

    rows = (await db.execute(select(CompanyRelationship))).scalars().all()
    ours = [
        r
        for r in rows
        if {r.company_id, r.related_company_id} <= {surv.id, loser.id, other.id}
    ]
    # Nothing touching the loser survives.
    assert all(
        loser.id not in (r.company_id, r.related_company_id) for r in ours
    )
    # The survivor's edge to a third company is untouched.
    assert any(
        r.company_id == surv.id and r.related_company_id == other.id for r in ours
    )
    # The loser company itself is gone.
    assert await db.get(Company, loser.id) is None
