"""Round-trip tests for M2 schema additions: RawPage model + Company enrichment columns.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via `alembic upgrade head`.

Tests are skipped when DATABASE_URL is unset or empty so they do not fail in
environments without a Postgres instance (e.g., CI without the service container).
"""

import os
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage

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


def make_raw_page(company_id: UUID, **kwargs: object) -> RawPage:
    defaults: dict[str, object] = {
        "company_id": company_id,
        "url": "https://acme.example.com",
        "content": "<html><body>Acme homepage</body></html>",
    }
    defaults.update(kwargs)
    return RawPage(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_insert_and_read_raw_page(db: AsyncSession) -> None:
    """RawPage can be inserted and read back; fetched_at is populated by server default."""
    company = make_company()
    db.add(company)
    await db.flush()

    page = make_raw_page(company_id=company.id)
    db.add(page)
    await db.flush()

    fetched = await db.get(RawPage, page.id)
    assert fetched is not None
    assert isinstance(fetched.id, UUID)
    assert fetched.company_id == company.id
    assert fetched.url == "https://acme.example.com"
    assert fetched.content == "<html><body>Acme homepage</body></html>"
    # server default must populate fetched_at
    assert fetched.fetched_at is not None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


async def test_raw_page_unique_constraint(db: AsyncSession) -> None:
    """Inserting two RawPages with the same (company_id, url) raises IntegrityError."""
    company = make_company(slug="raw-page-dupe", normalized_name="raw page dupe")
    db.add(company)
    await db.flush()

    page1 = make_raw_page(company_id=company.id)
    db.add(page1)
    await db.flush()

    page2 = make_raw_page(
        company_id=company.id,
        url="https://acme.example.com",  # same URL — must violate unique constraint
        content="<html>other content</html>",
    )
    db.add(page2)
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_company_tags_round_trip(db: AsyncSession) -> None:
    """Company.tags (ARRAY(String)) round-trips as a Python list."""
    company = make_company(
        slug="tagged-co",
        normalized_name="tagged co",
        name="Tagged Co",
        tags=["seed", "developer-tools"],
    )
    db.add(company)
    await db.flush()

    fetched = await db.get(Company, company.id)
    assert fetched is not None
    assert fetched.tags == ["seed", "developer-tools"]
    assert isinstance(fetched.tags, list)


async def test_company_last_enriched_payload_round_trip(db: AsyncSession) -> None:
    """Company.last_enriched_payload (JSONB) round-trips as a Python dict."""
    payload: dict[str, object] = {"foo": [1, 2], "bar": {"nested": True}}
    company = make_company(
        slug="payload-co",
        normalized_name="payload co",
        name="Payload Co",
        last_enriched_payload=payload,
    )
    db.add(company)
    await db.flush()

    fetched = await db.get(Company, company.id)
    assert fetched is not None
    assert fetched.last_enriched_payload == payload
    assert fetched.last_enriched_payload["foo"] == [1, 2]  # type: ignore[index]
