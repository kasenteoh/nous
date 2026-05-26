"""Round-trip tests for M1 SQLAlchemy models.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via `alembic upgrade head`.

Tests are skipped when DATABASE_URL is unset or empty so they do not fail in
environments without a Postgres instance (e.g., CI without the service container).
"""

import os
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Filing, RelatedPerson

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
    }
    defaults.update(kwargs)
    return Company(**defaults)


def make_filing(company_id: UUID, **kwargs: object) -> Filing:
    defaults: dict[str, object] = {
        "company_id": company_id,
        "accession_number": "0001234567-24-000001",
        "filing_date": date(2024, 1, 15),
        "raw_data": {"entityName": "Acme Corp"},
    }
    defaults.update(kwargs)
    return Filing(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_insert_and_read_company(db: AsyncSession) -> None:
    """Company can be inserted and read back with all fields intact."""
    company = make_company(
        cik="0001234567",
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
    assert fetched.cik == "0001234567"
    assert fetched.hq_city == "San Francisco"
    assert fetched.slug == "acme-corp"
    # server defaults must be populated after flush
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


async def test_insert_and_read_filing(db: AsyncSession) -> None:
    """Filing can be inserted and read back with Decimal fields."""
    company = make_company()
    db.add(company)
    await db.flush()

    filing = make_filing(
        company_id=company.id,
        offering_amount_total=Decimal("5000000.00"),
        amount_sold=Decimal("2500000.00"),
        investors_count=12,
        minimum_investment=Decimal("25000.00"),
    )
    db.add(filing)
    await db.flush()

    fetched = await db.get(Filing, filing.id)
    assert fetched is not None
    assert isinstance(fetched.id, UUID)
    assert fetched.company_id == company.id
    assert fetched.accession_number == "0001234567-24-000001"
    assert fetched.offering_amount_total == Decimal("5000000.00")
    assert fetched.investors_count == 12
    assert fetched.raw_data == {"entityName": "Acme Corp"}
    assert fetched.created_at is not None


async def test_insert_and_read_related_person(db: AsyncSession) -> None:
    """RelatedPerson can be inserted with address dict and read back."""
    company = make_company()
    db.add(company)
    await db.flush()

    filing = make_filing(company_id=company.id)
    db.add(filing)
    await db.flush()

    person = RelatedPerson(
        company_id=company.id,
        filing_id=filing.id,
        name="Jane Doe",
        relationship="Executive Officer",
        address={"street1": "123 Main St", "city": "San Francisco", "stateOrCountry": "CA"},
    )
    db.add(person)
    await db.flush()

    fetched = await db.get(RelatedPerson, person.id)
    assert fetched is not None
    assert fetched.name == "Jane Doe"
    assert fetched.relationship == "Executive Officer"
    assert fetched.address is not None
    assert fetched.address["city"] == "San Francisco"
    assert fetched.created_at is not None


async def test_company_slug_unique_constraint(db: AsyncSession) -> None:
    """Inserting two companies with the same slug raises IntegrityError."""
    c1 = make_company(slug="dupe-slug", normalized_name="dupe one")
    c2 = make_company(slug="dupe-slug", normalized_name="dupe two", name="Dupe Co 2")
    db.add(c1)
    await db.flush()

    db.add(c2)
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_filing_accession_number_unique_constraint(db: AsyncSession) -> None:
    """Inserting two filings with the same accession_number raises IntegrityError."""
    c1 = make_company(slug="company-a", normalized_name="company a", name="Company A")
    c2 = make_company(slug="company-b", normalized_name="company b", name="Company B")
    db.add(c1)
    db.add(c2)
    await db.flush()

    f1 = make_filing(company_id=c1.id, accession_number="0001234567-24-999999")
    f2 = make_filing(company_id=c2.id, accession_number="0001234567-24-999999")
    db.add(f1)
    await db.flush()

    db.add(f2)
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_uuid_assigned_automatically(db: AsyncSession) -> None:
    """UUID primary key is auto-generated on insert (applied at flush time)."""
    company = make_company(slug="uuid-test", normalized_name="uuid test")
    db.add(company)
    await db.flush()
    assert isinstance(company.id, UUID)


async def test_tables_exist(db: AsyncSession) -> None:
    """Verify all three M1 tables exist via information_schema query."""
    result = await db.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('companies', 'filings', 'related_persons') "
            "ORDER BY table_name"
        )
    )
    tables = [row[0] for row in result.fetchall()]
    assert tables == ["companies", "filings", "related_persons"]
