"""DB-gated tests for migration 0033 + the embed-companies stage + the RPC.

Covers, against a real Postgres with pgvector (CI: the pgvector/pgvector:pg15
service image; the schema comes from `alembic upgrade head`):

- models/migration consistency: the 0033 columns round-trip through the ORM;
- stage selection: shown+described only, hash-idempotent, --limit bounded,
  re-embeds on description change, excluded companies never embedded;
- the Python/SQL hash parity the selection depends on (unicode included);
- the similar_companies() SQL function the web calls via supabase-js .rpc():
  cosine ordering, anchor self-exclusion, excluded-company exclusion,
  unembedded rows invisible, unembedded anchor -> zero rows.

The embedder is always the deterministic FakeEmbedder — no model download.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline.embed_companies import (
    EMBEDDING_DIM,
    build_embedding_text,
    embedding_text_hash,
    run_embed_companies,
)

from .test_embed_companies import FakeEmbedder

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


def _stamp_embedded(company: Company, vector: list[float]) -> None:
    """Set embedding + matching hash + timestamp, as the stage would."""
    company.embedding = vector
    company.embedding_text_hash = embedding_text_hash(
        build_embedding_text(
            company.name, company.description_short, company.description_long
        )
    )
    company.embedded_at = datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Migration 0033 <-> models consistency
# ---------------------------------------------------------------------------


async def test_embedding_columns_round_trip(db: AsyncSession) -> None:
    """The 0033 columns exist with types the ORM mapping can round-trip."""
    company = _make_company("embed-round-trip")
    _stamp_embedded(company, [0.25] * EMBEDDING_DIM)
    db.add(company)
    await db.commit()
    db.expire_all()

    fetched = (
        await db.execute(select(Company).where(Company.slug == "embed-round-trip"))
    ).scalar_one()
    assert fetched.embedding is not None
    assert len(list(fetched.embedding)) == EMBEDDING_DIM
    # vector(384) stores float32 — compare approximately.
    assert all(abs(float(x) - 0.25) < 1e-6 for x in fetched.embedding)
    assert fetched.embedding_text_hash is not None
    assert len(fetched.embedding_text_hash) == 64
    assert fetched.embedded_at is not None


async def test_embedding_column_is_vector_384_and_unindexed(db: AsyncSession) -> None:
    """Schema pin: vector(384) column, and deliberately NO vector index yet.

    Migration 0033 documents the exact-scan decision; if someone adds an
    ivfflat/hnsw index later this test should be updated alongside the
    documented threshold, not deleted.
    """
    col = (
        await db.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = 'companies'::regclass AND a.attname = 'embedding'"
            )
        )
    ).scalar_one()
    assert col == "vector(384)"

    vector_indexes = (
        await db.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'companies' "
                "AND (indexdef ILIKE '%ivfflat%' OR indexdef ILIKE '%hnsw%')"
            )
        )
    ).fetchall()
    assert vector_indexes == []


# ---------------------------------------------------------------------------
# Stage selection + idempotence
# ---------------------------------------------------------------------------


async def test_embeds_shown_described_companies_only(db: AsyncSession) -> None:
    shown = _make_company("embed-shown")
    excluded = _make_company("embed-excluded", exclusion_reason="not_a_startup")
    undescribed = _make_company(
        "embed-undescribed", description_short=None, description_long=None
    )
    db.add_all([shown, excluded, undescribed])
    await db.commit()

    embedder = FakeEmbedder()
    summary = await run_embed_companies(db, embedder)

    assert summary.companies_seen == 1
    assert summary.embedded == 1
    assert summary.errors == 0

    await db.refresh(shown)
    await db.refresh(excluded)
    await db.refresh(undescribed)
    assert shown.embedding is not None
    assert shown.embedded_at is not None
    assert shown.embedding_text_hash == embedding_text_hash(
        build_embedding_text(shown.name, shown.description_short, shown.description_long)
    )
    # Excluded companies are never embedded, so they can never surface as a
    # neighbor (defense in depth on top of the RPC's own filter).
    assert excluded.embedding is None
    assert undescribed.embedding is None


async def test_second_run_is_a_noop(db: AsyncSession) -> None:
    """Hash idempotence: once embedded, an unchanged row is never re-selected."""
    db.add(_make_company("embed-idempotent"))
    await db.commit()

    first = await run_embed_companies(db, FakeEmbedder())
    assert first.embedded == 1

    second_embedder = FakeEmbedder()
    second = await run_embed_companies(db, second_embedder)
    assert second.companies_seen == 0
    assert second.embedded == 0
    assert second_embedder.calls == []  # the model seam is never touched


async def test_reembeds_when_description_changes(db: AsyncSession) -> None:
    company = _make_company("embed-changed")
    db.add(company)
    await db.commit()

    await run_embed_companies(db, FakeEmbedder())
    await db.refresh(company)
    old_hash = company.embedding_text_hash
    old_vector = list(company.embedding or [])

    company.description_long = "A completely rewritten long description."
    db.add(company)
    await db.commit()

    summary = await run_embed_companies(db, FakeEmbedder())
    assert summary.embedded == 1

    await db.refresh(company)
    assert company.embedding_text_hash != old_hash
    assert list(company.embedding or []) != old_vector


async def test_limit_bounds_work_and_never_embedded_go_first(
    db: AsyncSession,
) -> None:
    stale = _make_company("embed-stale")
    _stamp_embedded(stale, [0.5] * EMBEDDING_DIM)
    stale.description_long = "Changed after embedding, so the hash is stale."
    fresh_a = _make_company("embed-fresh-a")
    fresh_b = _make_company("embed-fresh-b")
    db.add_all([stale, fresh_a, fresh_b])
    await db.commit()

    summary = await run_embed_companies(db, FakeEmbedder(), limit=2)
    assert summary.companies_seen == 2
    assert summary.embedded == 2

    # NULLS FIRST ordering: the two never-embedded rows won the bounded slots;
    # the stale-but-embedded row waits for the next run.
    await db.refresh(fresh_a)
    await db.refresh(fresh_b)
    assert fresh_a.embedding is not None
    assert fresh_b.embedding is not None

    remainder = await run_embed_companies(db, FakeEmbedder(), limit=2)
    assert remainder.embedded == 1  # the stale row; then converged


async def test_sql_hash_matches_python_hash_for_unicode(db: AsyncSession) -> None:
    """Parity pin: the SQL-side sha256 must equal embedding_text_hash().

    The selection compares the stored (Python-computed) hash against the
    SQL-recomputed one; if they ever diverge, rows either re-embed forever or
    never refresh. Unicode + embedded newlines are the risky cases.
    """
    company = _make_company(
        "embed-unicode",
        name="Zürich Aţomics 🚀",
        description_short="Line one.\nLine two — ünïcode.",
        description_long="Ces données précèdent l'été 2026.",
    )
    _stamp_embedded(company, [0.1] * EMBEDDING_DIM)
    db.add(company)
    await db.commit()

    # Hash matches -> not selected.
    noop = await run_embed_companies(db, FakeEmbedder())
    assert noop.companies_seen == 0

    # Corrupt the stored hash -> selected again (the != arm, not just IS NULL).
    company.embedding_text_hash = "0" * 64
    db.add(company)
    await db.commit()
    rerun = await run_embed_companies(db, FakeEmbedder())
    assert rerun.companies_seen == 1
    assert rerun.embedded == 1


# ---------------------------------------------------------------------------
# similar_companies() — the web's RPC read path (created in migration 0033)
# ---------------------------------------------------------------------------


async def test_similar_companies_orders_by_cosine_and_filters(
    db: AsyncSession,
) -> None:
    anchor = _make_company("sim-anchor")
    _stamp_embedded(anchor, _basis_vector(0))

    near = _make_company("sim-near")
    near_vec = _basis_vector(0)
    near_vec[1] = 0.3  # small off-axis component: closest neighbor
    _stamp_embedded(near, near_vec)

    far = _make_company("sim-far")
    _stamp_embedded(far, _basis_vector(1))  # orthogonal: similarity ~0

    excluded_near = _make_company("sim-excluded", exclusion_reason="manual")
    _stamp_embedded(excluded_near, _basis_vector(0))  # identical to anchor

    unembedded = _make_company("sim-unembedded")

    db.add_all([anchor, near, far, excluded_near, unembedded])
    await db.commit()

    rows = (
        await db.execute(
            text(
                "SELECT slug, similarity FROM similar_companies(:company_id, :n)"
            ).bindparams(company_id=anchor.id, n=10)
        )
    ).fetchall()

    slugs = [row[0] for row in rows]
    # Anchor self-excluded; excluded company never surfaces even with a
    # perfect-match vector; unembedded rows are invisible.
    assert anchor.slug not in slugs
    assert excluded_near.slug not in slugs
    assert unembedded.slug not in slugs
    # Cosine ordering: near before orthogonal, similarities descending.
    assert slugs[:2] == [near.slug, far.slug]
    similarities = [float(row[1]) for row in rows[:2]]
    assert similarities[0] > similarities[1]
    assert similarities[0] > 0.9  # near-identical direction
    assert abs(similarities[1]) < 1e-6  # orthogonal -> ~0


async def test_similar_companies_respects_match_count(db: AsyncSession) -> None:
    anchor = _make_company("simcount-anchor")
    _stamp_embedded(anchor, _basis_vector(0))
    neighbors = []
    for i in range(3):
        neighbor = _make_company(f"simcount-{i}")
        vec = _basis_vector(0)
        vec[1] = 0.1 * (i + 1)
        _stamp_embedded(neighbor, vec)
        neighbors.append(neighbor)
    db.add_all([anchor, *neighbors])
    await db.commit()

    rows = (
        await db.execute(
            text(
                "SELECT slug FROM similar_companies(:company_id, :n)"
            ).bindparams(company_id=anchor.id, n=2)
        )
    ).fetchall()
    assert len(rows) == 2


async def test_similar_companies_empty_for_unembedded_anchor(
    db: AsyncSession,
) -> None:
    """No embedding on the anchor -> zero rows (the web renders no section)."""
    anchor = _make_company("simnull-anchor")  # no embedding
    other = _make_company("simnull-other")
    _stamp_embedded(other, _basis_vector(0))
    db.add_all([anchor, other])
    await db.commit()

    rows = (
        await db.execute(
            text(
                "SELECT slug FROM similar_companies(:company_id, :n)"
            ).bindparams(company_id=anchor.id, n=5)
        )
    ).fetchall()
    assert rows == []
