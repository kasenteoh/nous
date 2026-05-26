"""Integration tests for nous.db.upsert.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via ``alembic upgrade head``.

Tests are skipped when DATABASE_URL is unset or empty so they do not fail in
environments without a Postgres instance (e.g., CI without the service container).
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nous.db.models import RelatedPerson
from nous.db.upsert import insert_filing_if_new, replace_related_persons, upsert_company
from nous.sources.form_d import FormD, FormDAddress, FormDRelatedPerson

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB integration tests",
)

# ---------------------------------------------------------------------------
# Session fixtures (mirrors test_db_models.py pattern)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=False
    )
    return factory


@pytest_asyncio.fixture()
async def db(session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    """Yield a session, rolling back after each test."""
    async with session_factory() as session:
        await session.begin_nested()
        yield session
        await session.rollback()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_form_d(
    *,
    cik: str = "0001858523",
    entity_name: str = "Persefoni AI Inc.",
    industry_group_type: str = "Technology - Computers",
    city: str = "Mesa",
    state: str = "AZ",
    accession_number: str = "0001858523-21-000001",
    filing_date: date = date(2021, 6, 1),
    related_persons: list[FormDRelatedPerson] | None = None,
) -> FormD:
    return FormD(
        accession_number=accession_number,
        cik=cik,
        entity_name=entity_name,
        industry_group_type=industry_group_type,
        principal_place_of_business=FormDAddress(city=city, state=state, country="US"),
        filing_date=filing_date,
        total_offering_amount=Decimal("5000000"),
        related_persons=related_persons or [],
    )


# ---------------------------------------------------------------------------
# upsert_company
# ---------------------------------------------------------------------------


async def test_upsert_company_insert_new_with_cik(db: AsyncSession) -> None:
    """A new FormD with a CIK inserts a fresh Company row."""
    form_d = _make_form_d(cik="0001000001", entity_name="Alpha Tech Inc.")
    company, created = await upsert_company(db, form_d)

    assert created is True
    assert company.id is not None
    assert company.cik == "0001000001"
    assert company.name == "Alpha Tech Inc."
    assert company.slug  # non-empty
    assert company.normalized_name == "alpha tech"


async def test_upsert_company_update_existing_by_cik(db: AsyncSession) -> None:
    """A second FormD with the same CIK updates the existing Company, not inserts."""
    form_d_1 = _make_form_d(
        cik="0001000002",
        entity_name="Beta Corp",
        city="Denver",
        state="CO",
        accession_number="0001000002-21-000001",
    )
    company_1, created_1 = await upsert_company(db, form_d_1)
    await db.flush()

    form_d_2 = _make_form_d(
        cik="0001000002",
        entity_name="Beta Corp",
        city="Austin",  # location changed
        state="TX",
        accession_number="0001000002-22-000001",
    )
    company_2, created_2 = await upsert_company(db, form_d_2)

    assert created_1 is True
    assert created_2 is False
    assert company_1.id == company_2.id
    assert company_2.hq_city == "Austin"
    assert company_2.hq_state == "TX"


async def test_upsert_company_match_by_normalized_name_when_cik_empty(db: AsyncSession) -> None:
    """When CIK is empty, an existing company is found by normalized_name."""
    # First insert without a CIK.
    form_d_1 = _make_form_d(
        cik="",
        entity_name="Gamma Solutions LLC",
        accession_number="0001000003-21-000001",
    )
    company_1, created_1 = await upsert_company(db, form_d_1)
    await db.flush()

    # Second filing, also no CIK, same normalized name.
    form_d_2 = _make_form_d(
        cik="",
        entity_name="Gamma Solutions LLC",
        accession_number="0001000003-22-000001",
    )
    company_2, created_2 = await upsert_company(db, form_d_2)

    assert created_1 is True
    assert created_2 is False
    assert company_1.id == company_2.id


async def test_upsert_company_backfills_cik_via_normalized_name(db: AsyncSession) -> None:
    """If a no-CIK row exists and a new filing supplies the CIK, it is backfilled."""
    form_d_1 = _make_form_d(
        cik="",
        entity_name="Delta Robotics Inc.",
        accession_number="0001000004-21-000001",
    )
    company_1, _ = await upsert_company(db, form_d_1)
    await db.flush()
    assert company_1.cik is None

    form_d_2 = _make_form_d(
        cik="0001000004",
        entity_name="Delta Robotics Inc.",
        accession_number="0001000004-22-000001",
    )
    company_2, created_2 = await upsert_company(db, form_d_2)

    assert created_2 is False
    assert company_1.id == company_2.id
    assert company_2.cik == "0001000004"


async def test_upsert_company_slug_collision_disambiguation(db: AsyncSession) -> None:
    """Two companies with the same normalized name get disambiguated slugs."""
    form_d_1 = _make_form_d(
        cik="0001111111",
        entity_name="Zephyr Corp",
        accession_number="0001111111-21-000001",
    )
    company_1, created_1 = await upsert_company(db, form_d_1)
    await db.flush()

    form_d_2 = _make_form_d(
        cik="0002222222",
        entity_name="Zephyr Corp",
        accession_number="0002222222-21-000001",
    )
    company_2, created_2 = await upsert_company(db, form_d_2)
    await db.flush()

    assert created_1 is True
    assert created_2 is True
    assert company_1.id != company_2.id
    # Both slugs must be valid and distinct.
    assert company_1.slug
    assert company_2.slug
    assert company_1.slug != company_2.slug


# ---------------------------------------------------------------------------
# insert_filing_if_new
# ---------------------------------------------------------------------------


async def test_insert_filing_if_new_first_time_returns_filing(db: AsyncSession) -> None:
    """First insert returns a Filing row."""
    form_d = _make_form_d(accession_number="0001000010-21-000001")
    company, _ = await upsert_company(db, form_d)
    await db.flush()

    filing = await insert_filing_if_new(db, company.id, form_d)

    assert filing is not None
    assert filing.accession_number == "0001000010-21-000001"
    assert filing.company_id == company.id
    assert filing.raw_data is not None


async def test_insert_filing_if_new_duplicate_returns_none(db: AsyncSession) -> None:
    """Second insert with same accession_number returns None (idempotent)."""
    form_d = _make_form_d(accession_number="0001000011-21-000001")
    company, _ = await upsert_company(db, form_d)
    await db.flush()

    first = await insert_filing_if_new(db, company.id, form_d)
    assert first is not None

    await db.flush()

    second = await insert_filing_if_new(db, company.id, form_d)
    assert second is None


# ---------------------------------------------------------------------------
# replace_related_persons
# ---------------------------------------------------------------------------


async def test_replace_related_persons_inserts_all(db: AsyncSession) -> None:
    """replace_related_persons inserts the expected rows."""
    persons = [
        FormDRelatedPerson(name="Alice Smith", relationship="Director"),
        FormDRelatedPerson(name="Bob Jones", relationship="Executive Officer"),
    ]
    form_d = _make_form_d(
        accession_number="0001000020-21-000001",
        related_persons=persons,
    )
    company, _ = await upsert_company(db, form_d)
    await db.flush()
    filing = await insert_filing_if_new(db, company.id, form_d)
    assert filing is not None
    await db.flush()

    count = await replace_related_persons(db, company.id, filing.id, persons)
    assert count == 2

    await db.flush()
    result = await db.execute(
        select(RelatedPerson).where(RelatedPerson.filing_id == filing.id)
    )
    rows = list(result.scalars().all())
    assert len(rows) == 2
    names = {r.name for r in rows}
    assert names == {"Alice Smith", "Bob Jones"}


async def test_replace_related_persons_idempotent(db: AsyncSession) -> None:
    """Calling replace_related_persons twice yields the same final set."""
    persons = [FormDRelatedPerson(name="Charlie Brown", relationship="Promoter")]
    form_d = _make_form_d(accession_number="0001000021-21-000001", related_persons=persons)
    company, _ = await upsert_company(db, form_d)
    await db.flush()
    filing = await insert_filing_if_new(db, company.id, form_d)
    assert filing is not None
    await db.flush()

    await replace_related_persons(db, company.id, filing.id, persons)
    await db.flush()

    # Run again with the same persons.
    count2 = await replace_related_persons(db, company.id, filing.id, persons)
    await db.flush()

    result = await db.execute(
        select(RelatedPerson).where(RelatedPerson.filing_id == filing.id)
    )
    rows = list(result.scalars().all())
    assert count2 == 1
    assert len(rows) == 1
    assert rows[0].name == "Charlie Brown"


async def test_replace_related_persons_empty_list(db: AsyncSession) -> None:
    """replace_related_persons with empty list deletes existing and inserts none."""
    persons = [FormDRelatedPerson(name="Existing Person", relationship="Director")]
    form_d = _make_form_d(accession_number="0001000022-21-000001", related_persons=persons)
    company, _ = await upsert_company(db, form_d)
    await db.flush()
    filing = await insert_filing_if_new(db, company.id, form_d)
    assert filing is not None
    await db.flush()

    # Insert one person first.
    await replace_related_persons(db, company.id, filing.id, persons)
    await db.flush()

    # Now replace with empty list.
    count = await replace_related_persons(db, company.id, filing.id, [])
    await db.flush()

    result = await db.execute(
        select(RelatedPerson).where(RelatedPerson.filing_id == filing.id)
    )
    rows = list(result.scalars().all())
    assert count == 0
    assert len(rows) == 0
