"""DB-gated integration tests for investor slug assignment.

Requires DATABASE_URL pointing at a Postgres with the schema at head (the
0018 migration adds investors.slug as NOT NULL + unique).

Coverage:
- upsert_investor assigns a slug derived from the display name.
- distinct firms whose names slugify to the same base get a deterministic,
  name_normalized-seeded suffix (collision disambiguation).
- the slug is stable: re-upserting the same investor reuses the row and never
  changes the slug; a fresh investor with the same name produces the same slug.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Investor
from nous.db.upsert import build_investor_slug, upsert_investor
from nous.util.slugify import slugify

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


@pytest.mark.asyncio
async def test_upsert_investor_assigns_slug(db: AsyncSession) -> None:
    """A newly created investor gets a slug derived from its display name."""
    name = f"Sequoia Capital {uuid.uuid4().hex[:6]}"
    investor, created = await upsert_investor(db, name=name)
    assert created is True
    assert investor.slug
    # slugify lowercases and hyphenates the FULL name; "Capital" is not a
    # stripped corporate suffix (only corp/inc/llc/holdings/lp/llp/etc. are),
    # so it stays in the slug. The slug must equal slugify(name) exactly.
    assert investor.slug == slugify(name)


@pytest.mark.asyncio
async def test_upsert_investor_disambiguates_colliding_slugs(
    db: AsyncSession,
) -> None:
    """Two DISTINCT firms whose display names slugify to the same base get
    distinct slugs.

    "Acme One" and "Acme-One" canonicalize differently (the dot/hyphen survives
    canonicalize_investor_name, so they are two separate investor rows) but both
    slugify to the same base "acme...-one". The second must get a deterministic
    name_normalized-seeded disambiguator rather than collide on slug.
    """
    tag = uuid.uuid4().hex[:6]
    first, c1 = await upsert_investor(db, name=f"Acme{tag} One")
    second, c2 = await upsert_investor(db, name=f"Acme{tag}-One")

    assert c1 is True and c2 is True
    assert first.id != second.id  # distinct canonical names → distinct rows
    assert slugify(f"Acme{tag} One") == slugify(f"Acme{tag}-One")  # same base
    assert first.slug != second.slug
    # The second carries a 6-hex disambiguator appended to the shared base.
    assert second.slug.startswith(f"{first.slug}-")


@pytest.mark.asyncio
async def test_build_investor_slug_is_deterministic(db: AsyncSession) -> None:
    """Same (name, name_normalized) → same slug across calls (no randomness).

    Seeding the disambiguator from name_normalized (not os.urandom) is what
    makes the in-migration backfill and the live insert path agree.
    """
    tag = uuid.uuid4().hex[:6]
    # Occupy the base slug so the next call must disambiguate.
    base_name = f"Founders{tag} Fund"
    occupant, _ = await upsert_investor(db, name=base_name)

    slug_a = await build_investor_slug(
        db, name=f"Founders{tag} Group", name_normalized=f"founders{tag} group"
    )
    slug_b = await build_investor_slug(
        db, name=f"Founders{tag} Group", name_normalized=f"founders{tag} group"
    )
    assert slug_a == slug_b
    assert slug_a != occupant.slug


@pytest.mark.asyncio
async def test_upsert_investor_reuse_keeps_slug_stable(db: AsyncSession) -> None:
    """Re-upserting an existing investor returns the same row and slug."""
    name = f"Lightspeed {uuid.uuid4().hex[:6]} Venture Partners"
    first, c1 = await upsert_investor(db, name=name)
    await db.flush()
    # Different casing canonicalizes to the same key → same row, same slug.
    second, c2 = await upsert_investor(db, name=name.upper())

    assert c1 is True and c2 is False
    assert first.id == second.id
    assert first.slug == second.slug


@pytest.mark.asyncio
async def test_investor_slug_unique_across_distinct_firms(db: AsyncSession) -> None:
    """Distinct firms colliding on a base slug each get a unique slug.

    Three display names that all slugify to the same base but canonicalize
    differently (so they are three separate rows) must yield three distinct,
    non-null slugs.
    """
    tag = uuid.uuid4().hex[:6]
    names = [f"Bench{tag} Mark", f"Bench{tag}-Mark", f"Bench{tag}.Mark"]
    # All three share one base slug but are three distinct canonical names.
    assert len({slugify(n) for n in names}) == 1

    seen: set[str] = set()
    for n in names:
        inv, created = await upsert_investor(db, name=n)
        assert created is True
        assert inv.slug and inv.slug not in seen
        seen.add(inv.slug)
    assert len(seen) == 3
    _ = Investor  # keep import meaningful for readers
