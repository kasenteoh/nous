"""Round-trip tests for core SQLAlchemy models.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via `alembic upgrade head`.

Tests are skipped when DATABASE_URL is unset or empty so they do not fail in
environments without a Postgres instance (e.g., CI without the service container).
"""

import os
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def make_company(**kwargs: object) -> Company:
    defaults: dict[str, object] = {
        "name": "Acme Corp",
        "slug": "acme-corp",
        "normalized_name": "acme corp",
        "hq_country": "US",
        "discovered_via": "vc_portfolio",
    }
    defaults.update(kwargs)
    return Company(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_insert_and_read_company(db: AsyncSession) -> None:
    """Company can be inserted and read back with all fields intact."""
    company = make_company(
        hq_city="San Francisco",
        hq_state="CA",
        year_incorporated=2020,
        industry_group="Software",
    )
    db.add(company)
    await db.flush()

    fetched = await db.get(Company, company.id)
    assert fetched is not None
    assert isinstance(fetched.id, UUID)
    assert fetched.name == "Acme Corp"
    assert fetched.hq_city == "San Francisco"
    assert fetched.slug == "acme-corp"
    # server defaults must be populated after flush
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


async def test_company_slug_unique_constraint(db: AsyncSession) -> None:
    """Inserting two companies with the same slug raises IntegrityError."""
    c1 = make_company(slug="dupe-slug", normalized_name="dupe one")
    c2 = make_company(slug="dupe-slug", normalized_name="dupe two", name="Dupe Co 2")
    db.add(c1)
    await db.flush()

    db.add(c2)
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_uuid_assigned_automatically(db: AsyncSession) -> None:
    """UUID primary key is auto-generated on insert (applied at flush time)."""
    company = make_company(slug="uuid-test", normalized_name="uuid test")
    db.add(company)
    await db.flush()
    assert isinstance(company.id, UUID)


async def test_companies_table_exists(db: AsyncSession) -> None:
    """Verify the companies table exists and the dropped Form D tables do not."""
    result = await db.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('companies', 'filings', 'related_persons') "
            "ORDER BY table_name"
        )
    )
    tables = [row[0] for row in result.fetchall()]
    assert tables == ["companies"]
