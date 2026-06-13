"""Integration tests for the derive-relationships pipeline stage.

Requires DATABASE_URL (schema applied via ``alembic upgrade head``).
Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanyRelationship, Competitor
from nous.pipeline.derive_relationships import run_derive_relationships

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _company(
    name: str,
    slug: str,
    *,
    industry_group: str | None = None,
    primary_category: str | None = None,
    tags: list[str] | None = None,
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        industry_group=industry_group,
        primary_category=primary_category,
        tags=tags,
    )


async def _seed(db: AsyncSession) -> dict[str, Company]:
    a = _company(
        "Aco", "der-a",
        industry_group="devtools", primary_category="ci",
        tags=["python", "testing", "ci"],
    )
    b = _company(
        "Bco", "der-b",
        industry_group="devtools", primary_category="ci",
        tags=["python", "testing"],  # shares python+testing + category with A
    )
    c = _company(
        "Cco", "der-c",
        industry_group="fintech", primary_category="payments",
        tags=["payments"],  # different industry — never similar to A/B
    )
    d = _company(
        "Dco", "der-d",
        industry_group="devtools", primary_category="db",
        tags=["go"],  # same industry as A/B but 0 shared tags + different cat
    )
    db.add_all([a, b, c, d])
    await db.flush()
    # A resolved competitor edge: A lists B as its #1 competitor.
    db.add(
        Competitor(
            company_id=a.id,
            competitor_company_id=b.id,
            competitor_name="Bco",
            rank=1,
            source="techcrunch",
            reasoning="head to head in CI",
        )
    )
    await db.commit()
    return {"a": a, "b": b, "c": c, "d": d}


def _edges(rows: list[CompanyRelationship], ids: set[object]) -> list[CompanyRelationship]:
    return [r for r in rows if {r.company_id, r.related_company_id} <= ids]


async def test_derive_competitor_and_similar_edges(db: AsyncSession) -> None:
    co = await _seed(db)
    ids = {co["a"].id, co["b"].id, co["c"].id, co["d"].id}

    summary = await run_derive_relationships(db)
    assert summary.competitor_edges >= 1
    assert summary.similar_edges >= 2

    rows = _edges((await db.execute(select(CompanyRelationship))).scalars().all(), ids)

    # Competitor edge A -> B, sourced from competitors, rank-1 score ~1.0.
    competitor = [r for r in rows if r.relationship_type == "competitor"]
    assert any(
        r.company_id == co["a"].id and r.related_company_id == co["b"].id
        and r.source == "competitors" and float(r.score) == pytest.approx(1.0)
        for r in competitor
    )

    # Similar edges A<->B (bidirectional), sourced from industry_tags.
    similar = {
        (r.company_id, r.related_company_id)
        for r in rows
        if r.relationship_type == "similar"
    }
    assert (co["a"].id, co["b"].id) in similar
    assert (co["b"].id, co["a"].id) in similar

    # C (different industry) and D (same industry, 0 overlap) have no similar edges.
    similar_companies = {r.company_id for r in rows if r.relationship_type == "similar"}
    assert co["c"].id not in similar_companies
    assert co["d"].id not in similar_companies


async def test_derive_is_idempotent(db: AsyncSession) -> None:
    co = await _seed(db)
    ids = {co["a"].id, co["b"].id, co["c"].id, co["d"].id}

    first = await run_derive_relationships(db)
    rows_after_first = len(
        _edges((await db.execute(select(CompanyRelationship))).scalars().all(), ids)
    )
    second = await run_derive_relationships(db)
    rows_after_second = len(
        _edges((await db.execute(select(CompanyRelationship))).scalars().all(), ids)
    )

    assert first.competitor_edges == second.competitor_edges
    assert first.similar_edges == second.similar_edges
    assert rows_after_first == rows_after_second  # replace-style, no duplication


async def test_derive_dry_run_writes_nothing(db: AsyncSession) -> None:
    co = await _seed(db)
    ids = {co["a"].id, co["b"].id, co["c"].id, co["d"].id}

    summary = await run_derive_relationships(db, dry_run=True)
    assert summary.competitor_edges >= 1
    assert summary.similar_edges >= 2

    rows = _edges((await db.execute(select(CompanyRelationship))).scalars().all(), ids)
    assert rows == []  # dry-run must not write


async def test_derive_respects_max_similar_per_company(db: AsyncSession) -> None:
    # 4 companies in one industry all sharing a tag → each could link to 3 peers;
    # cap at 1 and assert no company has more than 1 similar edge.
    members = [
        _company(
            f"Maxco{i}", f"der-max-{i}",
            industry_group="maxgroup", primary_category="x",
            tags=["shared", f"t{i}"],
        )
        for i in range(4)
    ]
    db.add_all(members)
    await db.commit()
    member_ids = {m.id for m in members}

    await run_derive_relationships(db, max_similar_per_company=1)

    rows = [
        r
        for r in (await db.execute(select(CompanyRelationship))).scalars().all()
        if r.relationship_type == "similar" and r.company_id in member_ids
    ]
    per_company: dict[object, int] = {}
    for r in rows:
        per_company[r.company_id] = per_company.get(r.company_id, 0) + 1
    assert per_company  # at least some edges
    assert all(count <= 1 for count in per_company.values())


async def test_derive_projects_multiple_competitor_edges(db: AsyncSession) -> None:
    """Each projected competitor edge must get its OWN id.

    INSERT...FROM SELECT can't apply the model's per-row uuid4 default, so a
    naive projection reused one id for every row and the 2nd edge violated the
    PK — which rolled back the whole derive and left the competitor graph empty
    in prod (995 resolved competitors). The happy-path test only seeded ONE
    competitor, so it passed; this seeds several to guard the regression.
    """
    a = _company("MultiA", "der-multi-a", industry_group="x")
    b = _company("MultiB", "der-multi-b", industry_group="x")
    c = _company("MultiC", "der-multi-c", industry_group="x")
    db.add_all([a, b, c])
    await db.flush()
    db.add_all(
        [
            Competitor(
                company_id=a.id, competitor_company_id=b.id,
                competitor_name="MultiB", rank=1, source="llm_inferred",
            ),
            Competitor(
                company_id=a.id, competitor_company_id=c.id,
                competitor_name="MultiC", rank=2, source="llm_inferred",
            ),
            Competitor(
                company_id=b.id, competitor_company_id=a.id,
                competitor_name="MultiA", rank=1, source="llm_inferred",
            ),
        ]
    )
    await db.commit()

    summary = await run_derive_relationships(db)
    assert summary.competitor_edges == 3

    edges = [
        r
        for r in (await db.execute(select(CompanyRelationship))).scalars().all()
        if r.relationship_type == "competitor"
        and r.company_id in {a.id, b.id, c.id}
    ]
    pairs = {(e.company_id, e.related_company_id) for e in edges}
    assert pairs == {(a.id, b.id), (a.id, c.id), (b.id, a.id)}
    assert len({e.id for e in edges}) == 3  # distinct PKs — the actual regression
