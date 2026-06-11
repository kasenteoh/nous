"""Integration tests for nous.db.upsert.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via ``alembic upgrade head``.

Tests are skipped when DATABASE_URL is unset or empty so they do not fail in
environments without a Postgres instance (e.g., CI without the service container).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Person, RawPage
from nous.db.upsert import replace_people, upsert_raw_page
from nous.llm.prompts.company_description import PersonExtraction

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# upsert_raw_page
# ---------------------------------------------------------------------------


def _make_test_company(slug_suffix: str = "upsert") -> Company:
    return Company(
        name=f"TestCo {slug_suffix}",
        slug=f"testco-{slug_suffix}",
        normalized_name=f"testco {slug_suffix}",
        hq_country="US",
        discovered_via="vc_portfolio",
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


# ---------------------------------------------------------------------------
# replace_people
# ---------------------------------------------------------------------------


async def _people_for(db: AsyncSession, company_id: object) -> list[Person]:
    result = await db.execute(
        select(Person).where(Person.company_id == company_id).order_by(Person.rank)
    )
    return list(result.scalars().all())


async def test_replace_people_inserts_ranked_rows(db: AsyncSession) -> None:
    company = _make_test_company("people-insert")
    db.add(company)
    await db.flush()

    n = await replace_people(
        db,
        company.id,
        [
            PersonExtraction(name="Ada Lovelace", role="CEO"),
            PersonExtraction(name="Alan Turing", role="CTO"),
        ],
        source_url="https://acme.example/",
    )
    await db.flush()

    assert n == 2
    rows = await _people_for(db, company.id)
    assert [(r.name, r.role, r.rank) for r in rows] == [
        ("Ada Lovelace", "CEO", 1),
        ("Alan Turing", "CTO", 2),
    ]
    assert all(r.source_url == "https://acme.example/" for r in rows)


async def test_replace_people_is_idempotent(db: AsyncSession) -> None:
    company = _make_test_company("people-idem")
    db.add(company)
    await db.flush()

    people = [PersonExtraction(name="Grace Hopper", role="Founder")]
    await replace_people(db, company.id, people, source_url=None)
    await db.flush()
    await replace_people(db, company.id, people, source_url=None)
    await db.flush()

    rows = await _people_for(db, company.id)
    assert len(rows) == 1
    assert rows[0].name == "Grace Hopper"


async def test_replace_people_dedups_case_insensitive(db: AsyncSession) -> None:
    company = _make_test_company("people-dedup")
    db.add(company)
    await db.flush()

    n = await replace_people(
        db,
        company.id,
        [
            PersonExtraction(name="Ada Lovelace", role="CEO"),
            PersonExtraction(name="ada lovelace", role="Co-founder"),
        ],
        source_url=None,
    )
    await db.flush()

    assert n == 1  # second is a case-insensitive duplicate
    rows = await _people_for(db, company.id)
    assert len(rows) == 1
    assert rows[0].name == "Ada Lovelace"  # first-seen casing wins


async def test_replace_people_empty_clears(db: AsyncSession) -> None:
    company = _make_test_company("people-clear")
    db.add(company)
    await db.flush()

    await replace_people(
        db, company.id, [PersonExtraction(name="X", role="CEO")], source_url=None
    )
    await db.flush()

    n = await replace_people(db, company.id, [], source_url=None)
    await db.flush()

    assert n == 0
    rows = await _people_for(db, company.id)
    assert rows == []
