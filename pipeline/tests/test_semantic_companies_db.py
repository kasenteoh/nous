"""DB-gated tests for migration 0035 — the semantic_companies() RPC.

Covers, against a real Postgres with pgvector (CI: the pgvector/pgvector:pg15
service image; the schema comes from `alembic upgrade head`):

- cosine ordering against a caller-supplied query embedding (no anchor row —
  unlike similar_companies there is no self-exclusion because there is no
  self);
- excluded companies never surface, even with a perfect-match vector;
- unembedded rows are invisible;
- the catalog-bar defense-in-depth arm (an embedded row that somehow fails
  the bar is filtered);
- match_count caps the result;
- the returned columns are exactly the /companies card projection +
  similarity (web/lib/queries.ts::semanticCompanySearch narrows this shape).

Same conventions as test_embed_companies_db.py: the `db` fixture wraps every
test in a rolled-back outer transaction, and the module self-skips without
DATABASE_URL.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline.embed_companies import EMBEDDING_DIM

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(slug: str, **overrides: Any) -> Company:
    defaults: dict[str, Any] = {
        "name": f"Co {slug}",
        "slug": slug,
        "normalized_name": f"co {slug}",
        "description_short": f"Short description for {slug}.",
        "description_long": f"Long description for {slug}, with more detail.",
    }
    defaults.update(overrides)
    return Company(**defaults)


def _basis_vector(axis: int, dim: int = EMBEDDING_DIM) -> list[float]:
    vec = [0.0] * dim
    vec[axis] = 1.0
    return vec


def _embed(company: Company, vector: list[float]) -> None:
    company.embedding = vector
    company.embedded_at = datetime.now(tz=UTC)


def _vector_literal(vector: list[float]) -> str:
    """pgvector input literal — the same '[x,y,...]' string supabase-js sends."""
    return "[" + ",".join(str(x) for x in vector) + "]"


async def _call(
    db: AsyncSession, query_vector: list[float], match_count: int = 30
) -> list[Any]:
    return (
        await db.execute(
            text(
                "SELECT * FROM semantic_companies(CAST(:q AS vector), :n)"
            ).bindparams(q=_vector_literal(query_vector), n=match_count)
        )
    ).fetchall()


async def test_orders_by_cosine_and_filters(db: AsyncSession) -> None:
    near = _make_company("sem-near")
    near_vec = _basis_vector(0)
    near_vec[1] = 0.3  # small off-axis component: closest to the query
    _embed(near, near_vec)

    far = _make_company("sem-far")
    _embed(far, _basis_vector(1))  # orthogonal to the query: similarity ~0

    excluded = _make_company("sem-excluded", exclusion_reason="manual")
    _embed(excluded, _basis_vector(0))  # identical to the query

    unembedded = _make_company("sem-unembedded")

    db.add_all([near, far, excluded, unembedded])
    await db.commit()

    rows = await _call(db, _basis_vector(0))
    slugs = [row.slug for row in rows]

    assert excluded.slug not in slugs
    assert unembedded.slug not in slugs
    # Cosine ordering: near before orthogonal, similarities descending.
    assert slugs[:2] == [near.slug, far.slug]
    similarities = [float(row.similarity) for row in rows[:2]]
    assert similarities[0] > similarities[1]
    assert similarities[0] > 0.9  # near-identical direction
    assert abs(similarities[1]) < 1e-6  # orthogonal -> ~0


async def test_catalog_bar_defense_in_depth(db: AsyncSession) -> None:
    """An embedded row failing the bar is filtered even though the embed stage
    should never produce one (stage invariant: only described rows embed)."""
    bar_failing = _make_company(
        "sem-bar-failing", description_short=None, description_long=None
    )
    _embed(bar_failing, _basis_vector(0))
    passing = _make_company("sem-bar-passing")
    _embed(passing, _basis_vector(0))
    db.add_all([bar_failing, passing])
    await db.commit()

    slugs = [row.slug for row in await _call(db, _basis_vector(0))]
    assert bar_failing.slug not in slugs
    assert passing.slug in slugs


async def test_funding_only_row_passes_bar(db: AsyncSession) -> None:
    """The bar's other arm: no description but recorded funding still shows.

    (Unreachable via the embed stage today — it requires a description — but
    the SQL bar must mirror CATALOG_BAR_OR exactly, not approximate it.)
    """
    funded = _make_company(
        "sem-funded-only",
        description_short=None,
        description_long=None,
        funding_round_count=2,
    )
    _embed(funded, _basis_vector(0))
    db.add(funded)
    await db.commit()

    slugs = [row.slug for row in await _call(db, _basis_vector(0))]
    assert funded.slug in slugs


async def test_match_count_caps_results(db: AsyncSession) -> None:
    for i in range(4):
        company = _make_company(f"semcount-{i}")
        vec = _basis_vector(0)
        vec[1] = 0.1 * (i + 1)
        _embed(company, vec)
        db.add(company)
    await db.commit()

    rows = await _call(db, _basis_vector(0), match_count=2)
    assert len(rows) == 2


async def test_returns_card_projection_columns(db: AsyncSession) -> None:
    company = _make_company(
        "sem-projection",
        hq_city="Austin",
        hq_state="TX",
        industry_group="Developer Tools",
        logo_url="https://example.com/logo.png",
    )
    _embed(company, _basis_vector(0))
    db.add(company)
    await db.commit()

    rows = await _call(db, _basis_vector(0))
    row = next(r for r in rows if r.slug == "sem-projection")
    assert set(row._fields) == {
        "slug",
        "name",
        "hq_city",
        "hq_state",
        "industry_group",
        "description_short",
        "status",
        "logo_url",
        "similarity",
    }
    assert row.name == "Co sem-projection"
    assert row.hq_city == "Austin"
    assert row.hq_state == "TX"
    assert row.industry_group == "Developer Tools"
    assert row.status == "active"
    assert row.logo_url == "https://example.com/logo.png"
    assert float(row.similarity) == pytest.approx(1.0, abs=1e-6)


async def test_empty_catalog_returns_zero_rows(db: AsyncSession) -> None:
    rows = await _call(db, _basis_vector(0))
    assert rows == []
