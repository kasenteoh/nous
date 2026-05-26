"""Integration tests for the scrape-homepages pipeline stage.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via ``alembic upgrade head``.

Tests are skipped when DATABASE_URL is unset or empty.

A mock HomepageClient is used so no real HTTP calls are made.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.pipeline.scrape_homepages import run_scrape_homepages
from nous.sources.homepage import FetchResult, HomepageClient, RobotsBlockedError

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


def _make_company(
    *,
    name: str = "Acme Inc.",
    slug: str = "acme",
    website: str | None = "https://acme.com",
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        website=website,
    )


class MockHomepageClient(HomepageClient):
    """HomepageClient subclass returning canned responses per URL or raising errors."""

    def __init__(
        self,
        *,
        blocked_paths: set[str] | None = None,
        error_paths: set[str] | None = None,
        always_block: bool = False,
    ) -> None:
        """
        blocked_paths: URL substrings that raise RobotsBlockedError.
        error_paths: URL substrings that raise a generic httpx-like Exception.
        always_block: if True, every fetch raises RobotsBlockedError.
        """
        super().__init__(user_agent="test agent test@example.com")
        self._blocked_paths = blocked_paths or set()
        self._error_paths = error_paths or set()
        self._always_block = always_block

    async def __aenter__(self) -> MockHomepageClient:  # type: ignore[override]
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def fetch(self, url: str) -> FetchResult:
        if self._always_block:
            raise RobotsBlockedError(f"robots.txt blocked: {url}")
        for blocked in self._blocked_paths:
            if blocked in url:
                raise RobotsBlockedError(f"robots.txt blocked: {url}")
        for err_path in self._error_paths:
            if err_path in url:
                import httpx

                raise httpx.RequestError(f"network error: {url}", request=None)  # type: ignore[arg-type]
        return FetchResult(
            url=url,
            status_code=200,
            content=f"<html><body>Content for {url}</body></html>",
            content_type="text/html",
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_scrape_fetches_candidate_paths(db: AsyncSession) -> None:
    """For a company with a website, raw_pages are created for each candidate path."""
    company = _make_company(slug="scrape-basic", website="https://scrapebasic.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    summary = await run_scrape_homepages(db, client, max_pages_per_company=4)

    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    assert len(pages) == 4
    assert summary.pages_fetched >= 4
    assert summary.companies_seen >= 1


async def test_robots_blocked_url_is_skipped(db: AsyncSession) -> None:
    """RobotsBlockedError causes that URL to be skipped; other pages still fetched."""
    company = _make_company(slug="scrape-robots", website="https://scraperobotstest.com")
    db.add(company)
    await db.flush()
    await db.commit()

    # Block /about path.
    client = MockHomepageClient(blocked_paths={"/about"})
    summary = await run_scrape_homepages(db, client, max_pages_per_company=7)

    assert summary.pages_skipped_robots >= 1
    # Pages for other paths should still be fetched (there are 7 total candidate paths).
    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    # Should have fetched up to max_pages even with one blocked.
    assert len(pages) >= 3


async def test_network_error_is_counted_and_skipped(db: AsyncSession) -> None:
    """Network errors increment pages_failed and don't raise."""
    company = _make_company(slug="scrape-neterr", website="https://scrapeneterr.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient(error_paths={"/about"})
    summary = await run_scrape_homepages(db, client, max_pages_per_company=4)

    assert summary.pages_failed >= 1
    # Other pages were still fetched.
    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    assert len(pages) >= 1


async def test_max_pages_per_company_caps_fetches(db: AsyncSession) -> None:
    """max_pages_per_company stops fetching once the limit is reached."""
    company = _make_company(slug="scrape-maxpages", website="https://scrapemaxpages.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    summary = await run_scrape_homepages(db, client, max_pages_per_company=2)

    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    assert len(pages) == 2
    assert summary.pages_fetched == 2


async def test_company_without_website_is_skipped(db: AsyncSession) -> None:
    """Companies with no website are not scraped."""
    company = _make_company(
        slug="scrape-nowebsite",
        website=None,
    )
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    await run_scrape_homepages(db, client)

    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    assert len(pages) == 0


async def test_all_robots_blocked_increments_companies_with_no_pages(db: AsyncSession) -> None:
    """When all paths are blocked, companies_with_no_pages increments."""
    company = _make_company(slug="scrape-allblocked", website="https://scrapeallblocked.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient(always_block=True)
    summary = await run_scrape_homepages(db, client)

    assert summary.companies_with_no_pages >= 1


async def test_rerun_within_refetch_window_is_noop(db: AsyncSession) -> None:
    """Re-running the scrape immediately is idempotent (pages not refetched)."""
    company = _make_company(slug="scrape-idem", website="https://scrapeidem.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    # First run: fetch pages.
    s1 = await run_scrape_homepages(
        db, client, max_pages_per_company=2, refetch_after_days=90
    )
    assert s1.pages_fetched == 2

    # Second run immediately: should skip (pages are fresh, refetch_after_days=90).
    s2 = await run_scrape_homepages(
        db, client, max_pages_per_company=2, refetch_after_days=90
    )
    assert s2.companies_seen == 0

    # Total raw_pages should still be 2.
    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    assert len(pages) == 2


async def test_stale_pages_trigger_refetch(db: AsyncSession) -> None:
    """Pages older than refetch_after_days trigger a re-scrape."""
    from sqlalchemy import update

    company = _make_company(slug="scrape-stale", website="https://scrapestale.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    await run_scrape_homepages(db, client, max_pages_per_company=1, refetch_after_days=90)

    # Manually set fetched_at to be old.
    old = datetime.now(tz=UTC) - timedelta(days=200)
    await db.execute(
        update(RawPage)
        .where(RawPage.company_id == company.id)
        .values(fetched_at=old)
    )
    await db.commit()

    # Second run with refetch_after_days=90 should re-scrape.
    s2 = await run_scrape_homepages(
        db, client, max_pages_per_company=1, refetch_after_days=90
    )
    assert s2.companies_seen >= 1
    assert s2.pages_fetched >= 1
