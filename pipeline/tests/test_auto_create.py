"""Integration tests for M3 auto-create + fuzzy match helpers.

Covers:
- Exact normalized_name match returns the existing row.
- Trigram match above threshold returns the existing row.
- Trigram match below threshold inserts a new row.
- auto_create_company opportunistic website backfill on match.
- auto_create_company writes cik=NULL + discovered_via on insert.
- Re-running auto_create_company is idempotent.

Requires DATABASE_URL pointing at a Postgres with pg_trgm + migration 0003.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.db.upsert import auto_create_company, find_company_by_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_company(
    *,
    name: str,
    slug: str,
    normalized_name: str | None = None,
    website: str | None = None,
    discovered_via: str = "form_d",
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=normalized_name or name.lower(),
        hq_country="US",
        website=website,
        discovered_via=discovered_via,
    )


# ---------------------------------------------------------------------------
# find_company_by_name
# ---------------------------------------------------------------------------


async def test_find_by_name_exact_match(db: AsyncSession) -> None:
    existing = make_company(
        name="Ricursive Intelligence",
        slug=f"ricursive-intelligence-{os.urandom(3).hex()}",
        normalized_name="ricursive intelligence",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    found = await find_company_by_name(db, "Ricursive Intelligence")
    assert found is not None
    assert found.id == existing.id


async def test_find_by_name_trigram_above_threshold(db: AsyncSession) -> None:
    """A close-but-not-exact match still finds the row above the 0.85 default."""
    existing = make_company(
        name="Ricursive Intelligence Inc.",
        slug=f"ricursive-intelligence-{os.urandom(3).hex()}",
        normalized_name="ricursive intelligence",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    # "ricursive intelligence inc" → normalized_name "ricursive intelligence" —
    # exact match strips the suffix, so this hits exact, not trigram. Use a
    # genuinely different but close form instead.
    found = await find_company_by_name(db, "Ricursive Intelligenc")  # missing 'e'
    assert found is not None
    assert found.id == existing.id


async def test_find_by_name_trigram_below_threshold_returns_none(
    db: AsyncSession,
) -> None:
    """A name that's similar but below 0.85 should not match."""
    db.add(
        make_company(
            name="Acme Corp",
            slug=f"acme-corp-{os.urandom(3).hex()}",
            normalized_name="acme corp",
        )
    )
    await db.flush()
    await db.commit()

    # "wholly unrelated company" shares no trigrams with "acme corp"
    found = await find_company_by_name(db, "Wholly Unrelated Company")
    assert found is None


async def test_find_by_name_empty_returns_none(db: AsyncSession) -> None:
    found = await find_company_by_name(db, "")
    assert found is None


# ---------------------------------------------------------------------------
# auto_create_company
# ---------------------------------------------------------------------------


async def test_auto_create_inserts_new_row(db: AsyncSession) -> None:
    company, created = await auto_create_company(
        db,
        name="Brand New Co",
        website="https://brandnewco.example/",
        discovered_via="vc_portfolio",
    )
    assert created is True
    assert company.cik is None
    assert company.name == "Brand New Co"
    assert company.website == "https://brandnewco.example/"
    assert company.discovered_via == "vc_portfolio"
    assert company.description_short is None  # M2 enrichment will fill later
    assert company.hq_country == "US"


async def test_auto_create_returns_existing_on_exact_match(
    db: AsyncSession,
) -> None:
    existing = make_company(
        name="Existing Co",
        slug=f"existing-co-{os.urandom(3).hex()}",
        normalized_name="existing co",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    company, created = await auto_create_company(
        db,
        name="Existing Co",
        website="https://new-website.example/",
        discovered_via="vc_portfolio",
    )
    assert created is False
    assert company.id == existing.id


async def test_auto_create_backfills_website_when_missing(
    db: AsyncSession,
) -> None:
    """An existing row with no website gets the auto-create's URL filled in."""
    existing = make_company(
        name="No Site Co",
        slug=f"no-site-co-{os.urandom(3).hex()}",
        normalized_name="no site co",
        website=None,
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    _, created = await auto_create_company(
        db,
        name="No Site Co",
        website="https://nosite.example/",
        discovered_via="vc_portfolio",
    )
    assert created is False
    await db.commit()

    refetched = await db.get(Company, existing.id)
    assert refetched is not None
    assert refetched.website == "https://nosite.example/"


async def test_auto_create_does_not_overwrite_existing_website(
    db: AsyncSession,
) -> None:
    existing = make_company(
        name="Has Site Co",
        slug=f"has-site-co-{os.urandom(3).hex()}",
        normalized_name="has site co",
        website="https://resolved-by-m2.example/",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    await auto_create_company(
        db,
        name="Has Site Co",
        website="https://vc-claimed-url.example/",
        discovered_via="vc_portfolio",
    )
    await db.commit()

    refetched = await db.get(Company, existing.id)
    assert refetched is not None
    assert refetched.website == "https://resolved-by-m2.example/"


async def test_auto_create_idempotent_on_rerun(db: AsyncSession) -> None:
    """Calling auto_create_company twice for the same name yields one row."""
    company1, created1 = await auto_create_company(
        db,
        name="Idempotent Co",
        website=None,
        discovered_via="news",
    )
    assert created1 is True
    await db.commit()

    company2, created2 = await auto_create_company(
        db,
        name="Idempotent Co",
        website=None,
        discovered_via="news",
    )
    assert created2 is False
    assert company2.id == company1.id


async def test_auto_create_trigram_match_avoids_duplicate(
    db: AsyncSession,
) -> None:
    """A near-miss name (above 0.85 similarity) reuses the existing row."""
    existing = make_company(
        name="Ricursive Intelligence",
        slug=f"ricursive-intelligence-{os.urandom(3).hex()}",
        normalized_name="ricursive intelligence",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    _, created = await auto_create_company(
        db,
        name="Ricursive Intelligenc",  # typo: missing 'e' — should fuzzy-match
        website=None,
        discovered_via="news",
    )
    assert created is False
