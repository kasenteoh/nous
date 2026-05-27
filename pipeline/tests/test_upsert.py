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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage, RelatedPerson
from nous.db.upsert import (
    insert_filing_if_new,
    replace_related_persons,
    upsert_company,
    upsert_raw_page,
)
from nous.sources.form_d import FormD, FormDAddress, FormDRelatedPerson

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

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
    assert company.normalized_name == "alphatech"


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


async def test_upsert_company_preserves_first_seen_name_casing(
    db: AsyncSession,
) -> None:
    """Once a Company is created, the display ``name`` is sticky.

    SEC entity names are often ALL CAPS ("OPENAI, INC."). Overwriting a
    nicely-cased "OpenAI" would degrade the user-visible label. The fix
    leaves ``name`` and ``slug`` untouched on update and only refreshes
    ``normalized_name`` so future dedup keeps finding the row.
    """
    form_d_1 = _make_form_d(
        cik="0001234001",
        entity_name="OpenAI, Inc.",
        accession_number="0001234001-21-000001",
    )
    company_1, _ = await upsert_company(db, form_d_1)
    await db.flush()
    original_slug = company_1.slug

    form_d_2 = _make_form_d(
        cik="0001234001",
        entity_name="OPENAI, INC.",  # uppercase variant from a later SEC filing
        accession_number="0001234001-22-000001",
    )
    company_2, created_2 = await upsert_company(db, form_d_2)

    assert created_2 is False
    assert company_2.id == company_1.id
    # Name is preserved as first-seen, not downgraded to the shouting variant.
    assert company_2.name == "OpenAI, Inc."
    # Slug is sticky so URLs don't shift between weekly runs.
    assert company_2.slug == original_slug


async def test_upsert_company_preserves_non_form_d_name(
    db: AsyncSession,
) -> None:
    """When a VC-portfolio row already exists, a later Form D match must NOT
    overwrite ``name`` or ``discovered_via``. Only ``normalized_name`` and
    mutable fields (hq/industry) refresh."""
    from nous.util.slugify import normalize_name, slugify

    seeded = Company(
        cik=None,
        name="Stripe",
        slug=slugify("Stripe"),
        normalized_name=normalize_name("Stripe"),
        hq_country="US",
        discovered_via="vc_portfolio",
    )
    db.add(seeded)
    await db.flush()
    seeded_id = seeded.id
    seeded_slug = seeded.slug

    form_d = _make_form_d(
        cik="0001234500",
        entity_name="STRIPE, INC.",
        accession_number="0001234500-21-000001",
    )
    company, created = await upsert_company(db, form_d)

    assert created is False
    assert company.id == seeded_id
    # Form D backfilled CIK, but did NOT touch name/slug/discovered_via.
    assert company.cik == "0001234500"
    assert company.name == "Stripe"
    assert company.slug == seeded_slug
    assert company.discovered_via == "vc_portfolio"


async def test_upsert_company_does_not_hijack_non_form_d_row(
    db: AsyncSession,
) -> None:
    """Form D must NOT backfill CIK onto a row discovered by another source.

    Scenario: a VC-portfolio row exists for "Acme" (no CIK, discovered_via=
    'vc_portfolio'). SEC Form D arrives for a *different* "Acme" with a real
    CIK. Pre-fix, the CIK was backfilled onto the VC row, silently merging
    two distinct entities. After the fix, Form D inserts a new row.
    """
    from nous.util.slugify import normalize_name, slugify

    vc_row = Company(
        cik=None,
        name="Acme",
        slug=slugify("Acme"),
        normalized_name=normalize_name("Acme"),
        hq_country="US",
        discovered_via="vc_portfolio",
    )
    db.add(vc_row)
    await db.flush()
    vc_id = vc_row.id

    form_d = _make_form_d(
        cik="0009999001",
        entity_name="Acme",  # same normalized name
        accession_number="0009999001-26-000001",
    )
    company, created = await upsert_company(db, form_d)

    # A new row was inserted; the VC row is left alone.
    assert created is True
    assert company.id != vc_id
    assert company.cik == "0009999001"
    assert company.discovered_via == "form_d"

    # Verify VC row is untouched.
    refetched = await db.get(Company, vc_id)
    assert refetched is not None
    assert refetched.cik is None
    assert refetched.discovered_via == "vc_portfolio"


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


# ---------------------------------------------------------------------------
# upsert_raw_page
# ---------------------------------------------------------------------------


def _make_test_company(slug_suffix: str = "upsert") -> Company:
    return Company(
        name=f"TestCo {slug_suffix}",
        slug=f"testco-{slug_suffix}",
        normalized_name=f"testco {slug_suffix}",
        hq_country="US",
    )


async def test_upsert_raw_page_inserts_new_row(db: AsyncSession) -> None:
    """upsert_raw_page inserts a new RawPage and returns a populated ORM object."""
    company = _make_test_company("new")
    db.add(company)
    await db.flush()

    page = await upsert_raw_page(db, company.id, "https://example.com/", "<html>hello</html>")

    assert page.id is not None
    assert page.company_id == company.id
    assert page.url == "https://example.com/"
    assert page.content == "<html>hello</html>"
    assert page.fetched_at is not None


async def test_upsert_raw_page_updates_existing_row(db: AsyncSession) -> None:
    """upsert_raw_page with same (company_id, url) updates content in-place, leaving one row."""
    company = _make_test_company("update")
    db.add(company)
    await db.flush()

    url = "https://example.com/about"

    # First upsert.
    page1 = await upsert_raw_page(db, company.id, url, "<html>original</html>")
    await db.flush()

    # Second upsert — same key, different content.
    page2 = await upsert_raw_page(db, company.id, url, "<html>updated</html>")
    await db.flush()

    # Same UUID, content changed.
    assert page1.id == page2.id
    assert page2.content == "<html>updated</html>"

    # Only one row in the DB.
    result = await db.execute(
        select(RawPage).where(
            RawPage.company_id == company.id,
            RawPage.url == url,
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].content == "<html>updated</html>"


async def test_upsert_raw_page_different_urls_are_separate_rows(db: AsyncSession) -> None:
    """Different URLs for the same company produce distinct RawPage rows."""
    company = _make_test_company("multi-url")
    db.add(company)
    await db.flush()

    await upsert_raw_page(db, company.id, "https://example.com/", "<html>home</html>")
    await upsert_raw_page(db, company.id, "https://example.com/about", "<html>about</html>")
    await db.flush()

    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 2
