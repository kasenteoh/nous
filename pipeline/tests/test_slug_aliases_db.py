"""DB-gated integration tests for slug-alias recording in merge_companies.

Requires DATABASE_URL pointing at a Postgres with the schema at head
(migration 0032 creates ``slug_aliases``).

Coverage:
- A merge records (loser_slug → survivor_id).
- Chain convergence: merging A→B then B→C leaves BOTH a and b aliasing C —
  the repoint runs before the loser delete, so the CASCADE never eats a chain.
- Dedup-stage idempotency: a second run_dedup_companies pass changes nothing.
- Upsert idempotency: a freed-and-reissued slug that is merged again re-targets
  the existing alias row to the newest survivor (single row, updated).
- Shadow cleanup: an alias whose old_slug equals the survivor's own live slug
  is dropped by the merge rather than surviving as a latent self-redirect.

The pure guard (survivor's own slug never aliased) is covered without a DB in
test_slug_aliases.py.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, SlugAlias
from nous.db.upsert import merge_companies
from nous.pipeline.dedup_companies import run_dedup_companies
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(
    name: str,
    *,
    website: str | None = None,
    description_long: str | None = None,
    created_at: datetime | None = None,
) -> Company:
    suffix = os.urandom(4).hex()
    company = Company(
        name=name,
        slug=f"{normalize_name(name) or 'company'}-{suffix}",
        normalized_name=normalize_name(name),
        hq_country="US",
        website=website,
        description_long=description_long,
    )
    if created_at is not None:
        company.created_at = created_at
    return company


async def _all_aliases(session: AsyncSession) -> dict[str, object]:
    """Map of old_slug → company_id for every alias row."""
    rows = (await session.execute(select(SlugAlias))).scalars().all()
    return {row.old_slug: row.company_id for row in rows}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


async def test_merge_records_loser_slug_alias(db: AsyncSession) -> None:
    """Merging a loser into a survivor records (loser_slug → survivor_id) and
    never records the survivor's own slug."""
    survivor = _make_company("Acme Robotics", description_long="Robots.")
    loser = _make_company("Acme Inc")
    db.add_all([survivor, loser])
    await db.flush()
    loser_slug = loser.slug

    await merge_companies(db, survivor_id=survivor.id, loser_id=loser.id)
    await db.commit()

    aliases = await _all_aliases(db)
    assert aliases == {loser_slug: survivor.id}
    # The survivor's live slug is not an alias.
    assert survivor.slug not in aliases


# ---------------------------------------------------------------------------
# Chain convergence
# ---------------------------------------------------------------------------


async def test_merge_chain_converges_to_final_survivor(db: AsyncSession) -> None:
    """A→B then B→C leaves BOTH a and b aliasing C.

    The first merge records (a → B). The second merge must repoint that alias
    to C before B's row is deleted — relying on the FK CASCADE instead would
    silently destroy it — and add (b → C).
    """
    a = _make_company("Alpha Analytics")
    b = _make_company("Alpha Analytics Inc", description_long="Analytics.")
    c = _make_company("AlphaAnalytics Corp", description_long="Analytics HQ.")
    db.add_all([a, b, c])
    await db.flush()
    a_slug, b_slug = a.slug, b.slug

    await merge_companies(db, survivor_id=b.id, loser_id=a.id)
    await db.commit()
    assert await _all_aliases(db) == {a_slug: b.id}

    await merge_companies(db, survivor_id=c.id, loser_id=b.id)
    await db.commit()

    aliases = await _all_aliases(db)
    assert aliases == {a_slug: c.id, b_slug: c.id}


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_dedup_rerun_leaves_aliases_unchanged(db: AsyncSession) -> None:
    """Re-running the dedup stage after a domain merge is an alias no-op."""
    older = _make_company(
        "Acme Robotics",
        website="https://acme-dedup-alias.com",
        description_long="Acme builds warehouse robots.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = _make_company(
        "Acme Inc",
        website="https://www.acme-dedup-alias.com/home",
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    db.add_all([older, newer])
    await db.flush()
    await db.commit()
    newer_slug = newer.slug

    summary1 = await run_dedup_companies(db, llm_limit=0)
    assert summary1.domain_merges == 1
    aliases_after_first = await _all_aliases(db)
    assert aliases_after_first == {newer_slug: older.id}

    summary2 = await run_dedup_companies(db, llm_limit=0)
    assert summary2.domain_merges == 0
    assert await _all_aliases(db) == aliases_after_first


async def test_reissued_slug_retargets_existing_alias(db: AsyncSession) -> None:
    """ON CONFLICT (old_slug) DO UPDATE: when a slug that already has an alias
    row dies again (freed by the first merge's delete, reissued to a new
    company, then that company loses a later merge), the row re-targets to the
    newest survivor instead of erroring or keeping the stale target."""
    survivor1 = _make_company("Beta Systems", description_long="Beta v1.")
    loser1 = _make_company("Beta Sys")
    db.add_all([survivor1, loser1])
    await db.flush()
    reused_slug = loser1.slug

    await merge_companies(db, survivor_id=survivor1.id, loser_id=loser1.id)
    await db.commit()
    assert await _all_aliases(db) == {reused_slug: survivor1.id}

    # The delete freed reused_slug; a new company takes it, then loses a merge.
    survivor2 = _make_company("Gamma Grid", description_long="Gamma.")
    reissued = _make_company("Beta Sys")
    reissued.slug = reused_slug
    db.add_all([survivor2, reissued])
    await db.flush()

    await merge_companies(db, survivor_id=survivor2.id, loser_id=reissued.id)
    await db.commit()

    aliases = await _all_aliases(db)
    # Single row for the slug, now pointing at the newest survivor.
    assert aliases[reused_slug] == survivor2.id
    assert list(aliases).count(reused_slug) == 1


# ---------------------------------------------------------------------------
# Shadow cleanup
# ---------------------------------------------------------------------------


async def test_alias_shadowing_survivor_slug_is_dropped(db: AsyncSession) -> None:
    """An alias whose old_slug equals the survivor's own live slug is deleted
    by the merge (a repointed chain row could otherwise become a latent
    self-redirect: the alias's slug was freed by an old merge, then reissued
    to the company that is now the survivor)."""
    survivor = _make_company("Delta Data", description_long="Delta.")
    loser = _make_company("Delta Data Inc")
    db.add_all([survivor, loser])
    await db.flush()
    loser_slug = loser.slug

    # Simulate the corner: an old alias for the survivor's (reissued) slug,
    # currently pointing at the loser — after the repoint it would read
    # (survivor.slug → survivor.id), a self-alias.
    db.add(SlugAlias(old_slug=survivor.slug, company_id=loser.id))
    await db.flush()

    await merge_companies(db, survivor_id=survivor.id, loser_id=loser.id)
    await db.commit()

    aliases = await _all_aliases(db)
    assert survivor.slug not in aliases
    assert aliases == {loser_slug: survivor.id}
