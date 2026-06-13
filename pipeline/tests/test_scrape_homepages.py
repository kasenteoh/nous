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


async def test_scrape_fetches_homepage(db: AsyncSession) -> None:
    """For a company with a website, exactly one raw_page is created for ``/``."""
    company = _make_company(slug="scrape-basic", website="https://scrapebasic.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    summary = await run_scrape_homepages(db, client)

    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    assert len(pages) == 1
    assert pages[0].url.endswith("/")
    assert summary.pages_fetched == 1
    assert summary.companies_seen == 1


async def test_robots_blocked_homepage_is_skipped(db: AsyncSession) -> None:
    """RobotsBlockedError on ``/`` increments pages_skipped_robots and companies_with_no_pages."""
    company = _make_company(slug="scrape-robots", website="https://scraperobotstest.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient(always_block=True)
    summary = await run_scrape_homepages(db, client)

    assert summary.pages_skipped_robots == 1
    assert summary.companies_with_no_pages == 1
    assert summary.pages_fetched == 0

    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    assert len(pages) == 0


async def test_network_error_is_counted_and_skipped(db: AsyncSession) -> None:
    """Network errors on ``/`` increment pages_failed and companies_with_no_pages."""
    company = _make_company(slug="scrape-neterr", website="https://scrapeneterr.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient(error_paths={"/"})
    summary = await run_scrape_homepages(db, client)

    assert summary.pages_failed == 1
    assert summary.companies_with_no_pages == 1
    assert summary.pages_fetched == 0

    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    assert len(pages) == 0


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
    # First run: fetch the homepage.
    s1 = await run_scrape_homepages(db, client, refetch_after_days=90)
    assert s1.pages_fetched == 1

    # Second run immediately: should skip (page is fresh, refetch_after_days=90).
    s2 = await run_scrape_homepages(db, client, refetch_after_days=90)
    assert s2.companies_seen == 0

    # Total raw_pages should still be 1.
    result = await db.execute(
        select(RawPage).where(RawPage.company_id == company.id)
    )
    pages = result.scalars().all()
    assert len(pages) == 1


async def test_stale_pages_trigger_refetch(db: AsyncSession) -> None:
    """Pages older than refetch_after_days trigger a re-scrape."""
    from sqlalchemy import update

    company = _make_company(slug="scrape-stale", website="https://scrapestale.com")
    db.add(company)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    await run_scrape_homepages(db, client, refetch_after_days=90)

    # Manually set fetched_at to be old.
    old = datetime.now(tz=UTC) - timedelta(days=200)
    await db.execute(
        update(RawPage)
        .where(RawPage.company_id == company.id)
        .values(fetched_at=old)
    )
    await db.commit()

    # Second run with refetch_after_days=90 should re-scrape.
    s2 = await run_scrape_homepages(db, client, refetch_after_days=90)
    assert s2.companies_seen >= 1
    assert s2.pages_fetched >= 1


async def test_failed_scrape_backs_off_on_next_run(
    db: AsyncSession,
) -> None:
    """A company whose homepage fetch is permanently blocked must not be
    re-attempted within the failure back-off window.

    Pre-fix: scrape-homepages stored nothing on failure, so the eligibility
    query (no raw_pages OR stale) re-selected the same dead company every
    weekly run. After the fix, `last_scrape_attempt_at` is set on every
    attempt and the eligibility query honours `failure_backoff_days`.
    """
    company = _make_company(slug="dead-url-co", website="https://dead.example/")
    db.add(company)
    await db.flush()
    await db.commit()

    # First run: every fetch raises RobotsBlockedError.
    summary_1 = await run_scrape_homepages(
        db,
        MockHomepageClient(always_block=True),
        failure_backoff_days=30,
    )
    assert summary_1.companies_seen == 1
    assert summary_1.companies_with_no_pages == 1
    await db.commit()

    refetched = await db.get(Company, company.id)
    assert refetched is not None
    assert refetched.last_scrape_attempt_at is not None
    first_attempt = refetched.last_scrape_attempt_at

    # Second run immediately after — must skip this company entirely.
    summary_2 = await run_scrape_homepages(
        db,
        MockHomepageClient(),  # would succeed if called
        failure_backoff_days=30,
    )
    assert summary_2.companies_seen == 0  # eligibility excluded the dead row
    await db.commit()

    refetched_2 = await db.get(Company, company.id)
    assert refetched_2 is not None
    # The row was never selected, so the timestamp is unchanged.
    assert refetched_2.last_scrape_attempt_at == first_attempt


# ---------------------------------------------------------------------------
# Dead-site detection: consecutive_scrape_failures counter
# ---------------------------------------------------------------------------


async def test_failed_homepage_increments_failure_counter(db: AsyncSession) -> None:
    """A total homepage fetch failure bumps consecutive_scrape_failures by 1."""
    company = _make_company(
        slug="deadsite-incr", website="https://deadsite-incr.example/"
    )
    db.add(company)
    await db.flush()
    await db.commit()

    summary = await run_scrape_homepages(db, MockHomepageClient(error_paths={"/"}))
    assert summary.pages_failed == 1

    refetched = await db.get(Company, company.id)
    assert refetched is not None
    assert refetched.consecutive_scrape_failures == 1


async def test_consecutive_failures_accumulate_across_runs(db: AsyncSession) -> None:
    """Each failed scrape cycle adds one to the counter (back-off bypassed so
    the company stays eligible run-to-run)."""
    company = _make_company(
        slug="deadsite-accum", website="https://deadsite-accum.example/"
    )
    db.add(company)
    await db.flush()
    await db.commit()

    for expected in (1, 2, 3):
        await run_scrape_homepages(
            db,
            MockHomepageClient(error_paths={"/"}),
            failure_backoff_days=0,  # keep the dead company eligible every run
        )
        refetched = await db.get(Company, company.id)
        assert refetched is not None
        assert refetched.consecutive_scrape_failures == expected


async def test_successful_homepage_resets_failure_counter(db: AsyncSession) -> None:
    """A successful homepage fetch resets the counter to 0."""
    company = _make_company(
        slug="deadsite-reset", website="https://deadsite-reset.example/"
    )
    company.consecutive_scrape_failures = 4  # pretend prior runs failed
    db.add(company)
    await db.flush()
    await db.commit()

    summary = await run_scrape_homepages(db, MockHomepageClient())
    assert summary.pages_fetched == 1

    refetched = await db.get(Company, company.id)
    assert refetched is not None
    assert refetched.consecutive_scrape_failures == 0


async def test_robots_block_leaves_failure_counter_unchanged(db: AsyncSession) -> None:
    """A robots.txt block is not a dead site — the counter is untouched."""
    company = _make_company(
        slug="deadsite-robots", website="https://deadsite-robots.example/"
    )
    company.consecutive_scrape_failures = 2
    db.add(company)
    await db.flush()
    await db.commit()

    summary = await run_scrape_homepages(db, MockHomepageClient(always_block=True))
    assert summary.pages_skipped_robots == 1

    refetched = await db.get(Company, company.id)
    assert refetched is not None
    assert refetched.consecutive_scrape_failures == 2


# ---------------------------------------------------------------------------
# Runtime budget + stored-content semantics
# ---------------------------------------------------------------------------


async def test_max_runtime_zero_stops_before_first_company(db: AsyncSession) -> None:
    """max_runtime_minutes=0 exits cleanly before processing anything."""
    for i in range(3):
        db.add(
            _make_company(
                name=f"BudgetCo {i} Inc.",
                slug=f"budgetco-scrape-{i}",
                website=f"https://budgetco-scrape-{i}.com",
            )
        )
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    summary = await run_scrape_homepages(db, client, max_runtime_minutes=0)

    assert summary.companies_seen == 0
    assert summary.stopped_early is True


async def test_persisted_content_is_extracted_text_not_raw_html(
    db: AsyncSession,
) -> None:
    """raw_pages.content stores extracted visible text, not the raw HTML.

    Raw HTML for the full backlog (~9k pages × ~200KB) would blow Supabase's
    500MB free tier; every downstream consumer only reads visible text.
    """

    class HtmlClient(MockHomepageClient):
        async def fetch(self, url: str) -> FetchResult:
            return FetchResult(
                url=url,
                status_code=200,
                content=(
                    "<html><head><script>var hidden = 'no';</script></head>"
                    "<body><h1>Acme builds rockets</h1>"
                    "<p>Fast delivery to orbit.</p></body></html>"
                ),
                content_type="text/html",
            )

    company = _make_company(
        name="ExtractCo Inc.",
        slug="extractco-scrape",
        website="https://extractco-scrape.com",
    )
    db.add(company)
    await db.flush()
    await db.commit()

    client = HtmlClient()
    await run_scrape_homepages(db, client)

    pages = (
        (
            await db.execute(
                select(RawPage).where(RawPage.company_id == company.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(pages) >= 1
    for page in pages:
        assert "<" not in page.content  # no markup survives
        assert "var hidden" not in page.content  # scripts stripped
    assert any("Acme builds rockets" in page.content for page in pages)


async def test_persisted_content_is_capped(db: AsyncSession) -> None:
    """Pathologically large pages are truncated at the per-page cap."""
    from nous.pipeline.scrape_homepages import _MAX_STORED_CHARS

    big_paragraph = "word " * 60_000  # ~300k chars of visible text

    class BigPageClient(MockHomepageClient):
        async def fetch(self, url: str) -> FetchResult:
            return FetchResult(
                url=url,
                status_code=200,
                content=f"<html><body><p>{big_paragraph}</p></body></html>",
                content_type="text/html",
            )

    company = _make_company(
        name="BigPageCo Inc.",
        slug="bigpageco-scrape",
        website="https://bigpageco-scrape.com",
    )
    db.add(company)
    await db.flush()
    await db.commit()

    client = BigPageClient()
    await run_scrape_homepages(db, client)

    pages = (
        (
            await db.execute(
                select(RawPage).where(RawPage.company_id == company.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(pages) >= 1
    assert all(len(page.content) <= _MAX_STORED_CHARS for page in pages)


async def test_concurrent_batches_scrape_all_companies(db: AsyncSession) -> None:
    """concurrency=2 over 5 eligible companies (3 batches) scrapes every one.

    Guards the batched concurrent-fetch / sequential-persist path against
    dropping the tail and confirms each company's homepage is committed.
    """
    for i in range(5):
        db.add(
            _make_company(
                name=f"ScrapeBatch {i} Inc.",
                slug=f"scrape-batch-{i}",
                website=f"https://scrapebatch{i}.com",
            )
        )
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    summary = await run_scrape_homepages(db, client, concurrency=2)

    assert summary.companies_seen == 5
    assert summary.pages_fetched == 5  # one homepage each; mock HTML has no links
    assert summary.companies_with_no_pages == 0
    assert summary.stopped_early is False

    rows = await db.execute(
        select(RawPage.company_id)
        .join(Company, Company.id == RawPage.company_id)
        .where(Company.slug.like("scrape-batch-%"))
    )
    company_ids_with_pages = {row[0] for row in rows.all()}
    assert len(company_ids_with_pages) == 5
