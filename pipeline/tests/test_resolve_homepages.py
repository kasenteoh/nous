"""Integration tests for the resolve-homepages pipeline stage.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via ``alembic upgrade head``.

Tests are skipped when DATABASE_URL is unset or empty.

A mock HomepageClient is used so no real HTTP calls are made.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nous.db.models import Company
from nous.pipeline.resolve_homepages import run_resolve_homepages
from nous.sources.homepage import FetchResult, HomepageClient

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB integration tests",
)

# ---------------------------------------------------------------------------
# Session fixtures
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


def _make_company(
    *,
    name: str = "Acme Inc.",
    slug: str = "acme",
    website: str | None = None,
    website_resolved_at: datetime | None = None,
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        website=website,
        website_resolved_at=website_resolved_at,
    )


class MockHomepageClient(HomepageClient):
    """HomepageClient subclass that returns canned FetchResults without HTTP."""

    def __init__(self, resolve_map: dict[str, str | None]) -> None:
        """resolve_map: slug_base → resolved URL (or None if no match)."""
        super().__init__(user_agent="test agent test@example.com")
        self._resolve_map = resolve_map

    async def __aenter__(self) -> MockHomepageClient:  # type: ignore[override]
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def fetch(self, url: str) -> FetchResult:
        # Determine which slug_base this URL maps to by iterating known slugs.
        for slug_base, resolved in self._resolve_map.items():
            if resolved and url.startswith(f"https://{slug_base}"):
                return FetchResult(
                    url=resolved,
                    status_code=200,
                    content=f"<html><body>{slug_base} homepage</body></html>",
                    content_type="text/html",
                )
        raise Exception(f"MockHomepageClient: no match for {url}")


async def _make_resolve_client(
    slug_base: str, resolved_url: str | None
) -> MockHomepageClient:
    """Build a MockHomepageClient that resolves exactly one company."""
    return MockHomepageClient({slug_base: resolved_url})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_resolve_company_with_no_website(db: AsyncSession) -> None:
    """A company with no website gets one resolved when the .com matches."""
    company = _make_company(name="Acme Inc.", slug="acme-resolve-1")
    db.add(company)
    await db.flush()
    # Must commit because run_resolve_homepages commits inside the loop.
    await db.commit()

    client = MockHomepageClient({"acme": "https://acme.com/"})
    summary = await run_resolve_homepages(db, client)

    await db.refresh(company)
    assert company.website == "https://acme.com/"
    assert company.website_resolved_at is not None
    assert summary.websites_resolved >= 1


async def test_recent_website_resolved_at_is_skipped(db: AsyncSession) -> None:
    """A company with a recent website_resolved_at is NOT re-processed."""
    now = datetime.now(tz=UTC)
    company = _make_company(
        name="Freshco Inc.",
        slug="freshco-skip",
        website="https://freshco.com/",
        website_resolved_at=now - timedelta(days=1),  # very recent
    )
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient({})
    # Use a large refetch_after_days so the fresh company is excluded.
    summary = await run_resolve_homepages(db, client, refetch_after_days=90)

    assert summary.companies_seen == 0


async def test_stale_website_resolved_at_is_reprocessed(db: AsyncSession) -> None:
    """A company with a stale website_resolved_at IS re-processed."""
    old = datetime.now(tz=UTC) - timedelta(days=200)
    company = _make_company(
        name="Stale Inc.",
        slug="stale-reprocess",
        website="https://stale.com/",
        website_resolved_at=old,
    )
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient({"stale": "https://stale.com/"})
    summary = await run_resolve_homepages(db, client, refetch_after_days=90)

    assert summary.companies_seen >= 1


async def test_no_matching_tld_sets_resolved_at_but_not_website(db: AsyncSession) -> None:
    """When no TLD matches, website stays None but website_resolved_at is set."""
    company = _make_company(
        name="Nohomepage Inc.",
        slug="nohomepage-nomatch",
        website=None,
        website_resolved_at=None,
    )
    db.add(company)
    await db.flush()
    await db.commit()

    # Client has no mapping — fetch will raise and resolve_homepage returns None.
    client = MockHomepageClient({})
    summary = await run_resolve_homepages(db, client)

    await db.refresh(company)
    # website_resolved_at should be set (so we don't retry every run).
    assert company.website_resolved_at is not None
    # But website stays None since nothing resolved.
    assert company.website is None
    assert summary.no_match >= 1


async def test_rerun_is_noop(db: AsyncSession) -> None:
    """Running resolve_homepages twice in a row is idempotent."""
    company = _make_company(name="Idempotent Inc.", slug="idempotent-resolve")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient({"idempotent": "https://idempotent.com/"})
    await run_resolve_homepages(db, client)

    # After first run, website_resolved_at is set.
    await db.refresh(company)
    assert company.website_resolved_at is not None

    # Second run within the 90-day window: company should be skipped.
    summary2 = await run_resolve_homepages(db, client, refetch_after_days=90)

    # company_seen count should be 0 on the second run (already resolved recently).
    assert summary2.companies_seen == 0


async def test_limit_caps_companies_processed(db: AsyncSession) -> None:
    """The limit parameter caps the number of companies processed."""
    for i in range(3):
        company = _make_company(
            name=f"LimitCo {i} Inc.",
            slug=f"limitco-resolve-{i}",
            website=None,
            website_resolved_at=None,
        )
        db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient({})
    summary = await run_resolve_homepages(db, client, limit=1)

    assert summary.companies_seen == 1
