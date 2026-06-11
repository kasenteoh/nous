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

from nous.db.models import Company, RawPage
from nous.db.upsert import upsert_raw_page

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
