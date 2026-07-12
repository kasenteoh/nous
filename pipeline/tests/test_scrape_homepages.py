"""Integration tests for the scrape-homepages pipeline stage.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via ``alembic upgrade head``.

Tests are skipped when DATABASE_URL is unset or empty.

A mock HomepageClient is used so no real HTTP calls are made.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.pipeline.scrape_homepages import run_scrape_homepages
from nous.sources.headless_browser import HeadlessBrowserClient
from nous.sources.homepage import FetchResult, HomepageClient, RobotsBlockedError
from tests.test_browser_fallback import DEAD_ZONE_SHELL_HTML

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
    latest_round_amount: Decimal | None = None,
    funding_round_count: int = 0,
    description_short: str | None = None,
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        website=website,
        latest_round_amount=latest_round_amount,
        funding_round_count=funding_round_count,
        description_short=description_short,
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


async def test_limit_prioritizes_highest_raise(db: AsyncSession) -> None:
    """With a tight --limit, the highest-raise eligible companies are scraped
    first so marquee names get pages (and thus enrichment) ahead of the long
    tail."""
    big = _make_company(
        name="BigRaise Inc.",
        slug="bigraise-prio-scrape",
        website="https://bigraise-scrape.com",
        latest_round_amount=Decimal("500000000"),  # $500M
    )
    mid = _make_company(
        name="MidRaise Inc.",
        slug="midraise-prio-scrape",
        website="https://midraise-scrape.com",
        latest_round_amount=Decimal("10000000"),  # $10M
    )
    none = _make_company(
        name="NoRaise Inc.",
        slug="noraise-prio-scrape",
        website="https://noraise-scrape.com",
        latest_round_amount=None,  # no funding amount → sorts last
    )
    db.add_all([none, mid, big])  # add in non-priority order on purpose
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    # limit=2 admits only the two most prominent of the three.
    summary = await run_scrape_homepages(db, client, limit=2)

    assert summary.companies_seen == 2

    # The two highest-raise companies were scraped; the NULL-amount one was not.
    big_pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == big.id))
    ).scalars().all()
    mid_pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == mid.id))
    ).scalars().all()
    none_pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == none.id))
    ).scalars().all()
    assert len(big_pages) == 1
    assert len(mid_pages) == 1
    assert none_pages == []

    await db.refresh(none)
    # The low-priority company was never attempted this run.
    assert none.last_scrape_attempt_at is None


async def test_funding_round_count_breaks_amount_ties(db: AsyncSession) -> None:
    """When latest_round_amount ties, the company with more funding rounds is
    scraped first within the limited slot."""
    many_rounds = _make_company(
        name="ManyRounds Inc.",
        slug="manyrounds-tie-scrape",
        website="https://manyrounds-scrape.com",
        latest_round_amount=Decimal("10000000"),
        funding_round_count=5,
    )
    few_rounds = _make_company(
        name="FewRounds Inc.",
        slug="fewrounds-tie-scrape",
        website="https://fewrounds-scrape.com",
        latest_round_amount=Decimal("10000000"),  # same amount
        funding_round_count=1,
    )
    db.add_all([few_rounds, many_rounds])
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    summary = await run_scrape_homepages(db, client, limit=1)

    assert summary.companies_seen == 1
    many_pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == many_rounds.id))
    ).scalars().all()
    few_pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == few_rounds.id))
    ).scalars().all()
    assert len(many_pages) == 1
    assert few_pages == []
    await db.refresh(few_rounds)
    assert few_rounds.last_scrape_attempt_at is None


# ---------------------------------------------------------------------------
# Logo / favicon discovery during scrape
# ---------------------------------------------------------------------------


class LogoMockHomepageClient(MockHomepageClient):
    """MockHomepageClient that also exposes a real SSRF-guarded ``_client``
    (backed by a mock transport) so the favicon validation path runs.

    The homepage fetch returns ``homepage_html`` (which declares an
    apple-touch-icon); the validation HEAD/GET for the icon URL is answered by
    ``image_handler`` — letting a test simulate "candidate is a real image" or
    "candidate is HTML / 404" without touching the network.
    """

    def __init__(
        self,
        *,
        homepage_html: str,
        image_handler: object,
    ) -> None:
        super().__init__()
        self._homepage_html = homepage_html
        self._image_handler = image_handler

    async def __aenter__(self) -> LogoMockHomepageClient:  # type: ignore[override]
        # Wire a guarded-shaped client over a mock transport so _assert_open()
        # in the stage returns a usable client and fetch_logo_url can validate.
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._image_handler),  # type: ignore[arg-type]
            headers={"User-Agent": "test agent test@example.com"},
            follow_redirects=True,
        )
        self._robots = object()  # type: ignore[assignment]  # only _client is used by the logo path
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            url=url,
            status_code=200,
            content=self._homepage_html,
            content_type="text/html",
        )


_HOMEPAGE_WITH_ICON = (
    "<html><head>"
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
    "<title>Acme builds rockets</title>"
    "</head><body><h1>Acme</h1></body></html>"
)


@pytest.fixture()
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the SSRF guard's resolver to report a public IP for the synthetic
    test hosts (which don't exist in DNS), so the favicon validation reaches the
    mock transport instead of failing on an unresolvable host."""
    import nous.util.ssrf as ssrf_module

    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]  # public (example.com)

    monkeypatch.setattr(ssrf_module, "resolve_host_ips", fake_resolve)


async def test_scrape_stores_validated_logo_url(db: AsyncSession, _public_dns: None) -> None:
    """A homepage declaring an apple-touch-icon that resolves to a real image
    populates company.logo_url with the external (own-domain) URL."""

    def image_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 200,
            headers={"content-type": "image/png"},
        )

    company = _make_company(slug="logo-found", website="https://logofound.com")
    db.add(company)
    await db.flush()
    await db.commit()

    async with LogoMockHomepageClient(
        homepage_html=_HOMEPAGE_WITH_ICON, image_handler=image_handler
    ) as client:
        summary = await run_scrape_homepages(db, client)

    assert summary.logos_found == 1
    await db.refresh(company)
    assert company.logo_url == "https://logofound.com/apple-touch-icon.png"


async def test_scrape_leaves_logo_null_when_candidate_not_image(
    db: AsyncSession, _public_dns: None
) -> None:
    """When the icon candidate resolves to HTML (SPA catch-all), logo_url stays
    NULL — we never store a non-image as a logo."""

    def html_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><body>app shell</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    company = _make_company(slug="logo-nonimage", website="https://logononimage.com")
    db.add(company)
    await db.flush()
    await db.commit()

    async with LogoMockHomepageClient(
        homepage_html=_HOMEPAGE_WITH_ICON, image_handler=html_handler
    ) as client:
        summary = await run_scrape_homepages(db, client)

    assert summary.logos_found == 0
    assert summary.pages_fetched >= 1  # the scrape itself still succeeded
    await db.refresh(company)
    assert company.logo_url is None


async def test_scrape_does_not_clobber_existing_logo_on_miss(
    db: AsyncSession, _public_dns: None
) -> None:
    """A transient logo miss (candidate 404s) must not overwrite a logo that was
    already stored on a previous run."""

    def not_found_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    company = _make_company(slug="logo-keep", website="https://logokeep.com")
    company.logo_url = "https://logokeep.com/existing-logo.png"
    db.add(company)
    await db.flush()
    await db.commit()

    async with LogoMockHomepageClient(
        homepage_html=_HOMEPAGE_WITH_ICON, image_handler=not_found_handler
    ) as client:
        summary = await run_scrape_homepages(db, client)

    assert summary.logos_found == 0
    await db.refresh(company)
    # Unchanged — the prior logo survives a miss.
    assert company.logo_url == "https://logokeep.com/existing-logo.png"


async def test_logo_discovery_skipped_for_plain_mock_client(
    db: AsyncSession,
) -> None:
    """The plain MockHomepageClient (no real _client) must scrape normally with
    logo discovery silently skipped — proves logo lookup never breaks a scrape
    even when the client can't validate images."""
    company = _make_company(slug="logo-skip", website="https://logoskip.com")
    db.add(company)
    await db.flush()
    await db.commit()

    summary = await run_scrape_homepages(db, MockHomepageClient())

    assert summary.pages_fetched == 1
    assert summary.logos_found == 0
    await db.refresh(company)
    assert company.logo_url is None


# ---------------------------------------------------------------------------
# Husk rescue (H-1): selection priority, short refetch cycle, forced render
# ---------------------------------------------------------------------------

# Rendered DOM the mock browser "produces" for the dead-zone shell: >700 chars
# of visible text so the rescued page clears BOTH enrich gates (_MIN_TEXT_CHARS
# and _MIN_DESCRIBE_CHARS). No <a> tags — keeps the scrape to one page.
_RENDERED_RICH_HTML = (
    "<html><body><main><h1>Perplexity</h1><p>"
    + (
        "Perplexity is an AI-powered answer engine that searches the live web "
        "and responds with cited, conversational answers. "
        * 12
    )
    + "</p></main></body></html>"
)


class DeadZoneShellClient(MockHomepageClient):
    """Static fetch always succeeds (HTTP 200) but returns the SPA shell whose
    extracted text sits between the near-zero fallback trigger (200) and the
    describe threshold (700) — the Perplexity shape."""

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            url=url,
            status_code=200,
            content=DEAD_ZONE_SHELL_HTML,
            content_type="text/html",
        )


def _mock_browser(rendered_html: str = _RENDERED_RICH_HTML) -> AsyncMock:
    browser = AsyncMock(spec=HeadlessBrowserClient)
    browser.fetch_rendered_html = AsyncMock(return_value=rendered_html)
    return browser


async def test_husk_outranks_prominent_described_company(db: AsyncSession) -> None:
    """A shown, description-less company sorts ahead of a far more prominent
    already-described one: a bounded run spends its slots on rescues before
    routine refetches."""
    described = _make_company(
        name="BigDescribed Inc.",
        slug="husk-prio-described",
        website="https://husk-prio-described.com",
        latest_round_amount=Decimal("500000000"),  # $500M — would win on raise
        description_short="Already has a description.",
    )
    husk = _make_company(
        name="SmallHusk Inc.",
        slug="husk-prio-husk",
        website="https://husk-prio-husk.com",
        latest_round_amount=Decimal("1000000"),  # $1M
        description_short=None,
    )
    db.add_all([described, husk])
    await db.flush()
    await db.commit()

    summary = await run_scrape_homepages(db, MockHomepageClient(), limit=1)

    assert summary.companies_seen == 1
    husk_pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == husk.id))
    ).scalars().all()
    described_pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == described.id))
    ).scalars().all()
    assert len(husk_pages) == 1
    assert described_pages == []
    await db.refresh(described)
    assert described.last_scrape_attempt_at is None  # never attempted this run


async def test_husks_order_by_prominence_within_the_rescue_tier(
    db: AsyncSession,
) -> None:
    """Within the husk tier the standing prominence order still applies."""
    big_husk = _make_company(
        name="BigHusk Inc.",
        slug="husk-tier-big",
        website="https://husk-tier-big.com",
        latest_round_amount=Decimal("200000000"),
    )
    small_husk = _make_company(
        name="SmallerHusk Inc.",
        slug="husk-tier-small",
        website="https://husk-tier-small.com",
        latest_round_amount=Decimal("2000000"),
    )
    db.add_all([small_husk, big_husk])
    await db.flush()
    await db.commit()

    summary = await run_scrape_homepages(db, MockHomepageClient(), limit=1)

    assert summary.companies_seen == 1
    big_pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == big_husk.id))
    ).scalars().all()
    assert len(big_pages) == 1
    await db.refresh(small_husk)
    assert small_husk.last_scrape_attempt_at is None


async def test_husk_refetches_on_rescue_cycle(db: AsyncSession) -> None:
    """A husk with 10-day-old pages re-enters the selection under the default
    7-day rescue window even though the standing 90-day window hasn't elapsed."""
    husk = _make_company(
        slug="husk-rescue-cycle", website="https://husk-rescue-cycle.com"
    )
    db.add(husk)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    s1 = await run_scrape_homepages(db, client)
    assert s1.pages_fetched == 1

    ten_days_ago = datetime.now(tz=UTC) - timedelta(days=10)
    await db.execute(
        update(RawPage)
        .where(RawPage.company_id == husk.id)
        .values(fetched_at=ten_days_ago)
    )
    await db.commit()

    s2 = await run_scrape_homepages(db, client, refetch_after_days=90)
    assert s2.companies_seen == 1
    assert s2.pages_fetched >= 1


async def test_described_company_waits_out_the_full_window(
    db: AsyncSession,
) -> None:
    """The rescue cycle applies ONLY to description-less companies: a described
    company with 10-day-old pages stays on the 90-day cadence."""
    described = _make_company(
        slug="husk-cycle-described",
        website="https://husk-cycle-described.com",
        description_short="Described already.",
    )
    db.add(described)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    s1 = await run_scrape_homepages(db, client)
    assert s1.pages_fetched == 1

    ten_days_ago = datetime.now(tz=UTC) - timedelta(days=10)
    await db.execute(
        update(RawPage)
        .where(RawPage.company_id == described.id)
        .values(fetched_at=ten_days_ago)
    )
    await db.commit()

    s2 = await run_scrape_homepages(db, client, refetch_after_days=90)
    assert s2.companies_seen == 0


async def test_husk_with_fresh_pages_is_not_hammered(db: AsyncSession) -> None:
    """The rescue cycle is bounded: a husk scraped moments ago is NOT
    re-selected — at most one scrape set per rescue window."""
    husk = _make_company(
        slug="husk-rescue-bounded", website="https://husk-rescue-bounded.com"
    )
    db.add(husk)
    await db.flush()
    await db.commit()

    client = MockHomepageClient()
    s1 = await run_scrape_homepages(db, client)
    assert s1.pages_fetched == 1

    s2 = await run_scrape_homepages(db, client)
    assert s2.companies_seen == 0


async def test_forced_render_rescues_spa_shell_husk(db: AsyncSession) -> None:
    """Regression (the Perplexity shape): a husk whose static fetch succeeds
    (HTTP 200) with dead-zone-thin visible text now stores the headless-rendered
    text instead of the shell."""
    husk = _make_company(
        name="Perplexity-Shaped Inc.",
        slug="husk-forced-render",
        website="https://husk-forced-render.com",
    )
    db.add(husk)
    await db.flush()
    await db.commit()

    browser = _mock_browser()
    summary = await run_scrape_homepages(
        db, DeadZoneShellClient(), browser_client=browser
    )

    assert summary.pages_via_browser_fallback == 1
    browser.fetch_rendered_html.assert_awaited()
    pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == husk.id))
    ).scalars().all()
    assert len(pages) == 1
    # The stored content is the extracted RENDERED text, not the shell chips,
    # and it is long enough to clear enrich's judge + describe input gates.
    assert "answer engine that searches the live web" in pages[0].content
    assert len(pages[0].content) >= 700


async def test_described_company_keeps_dead_zone_static_content(
    db: AsyncSession,
) -> None:
    """A described company with the same dead-zone static text does NOT pay for
    a render — the raised threshold applies only to husks."""
    described = _make_company(
        slug="husk-render-described",
        website="https://husk-render-described.com",
        description_short="Described already.",
    )
    db.add(described)
    await db.flush()
    await db.commit()

    browser = _mock_browser()
    summary = await run_scrape_homepages(
        db, DeadZoneShellClient(), browser_client=browser
    )

    assert summary.pages_via_browser_fallback == 0
    browser.fetch_rendered_html.assert_not_called()
    pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == described.id))
    ).scalars().all()
    assert len(pages) == 1
    assert "answer engine that searches the live web" not in pages[0].content


async def test_rescued_husk_is_picked_up_by_enrich(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: after the forced render stores rich text, the UNCHANGED
    enrich selection (description_short IS NULL + a >=200-char page) picks the
    husk up on its next run and writes both descriptions."""
    from nous.llm.prompts.company_description import CompanyDescription
    from nous.llm.prompts.company_description_long import CompanyLongDescription
    from nous.pipeline.enrich_companies import run_enrich_companies

    husk = _make_company(
        name="RescuedHusk Inc.",
        slug="husk-enrich-pickup",
        website="https://husk-enrich-pickup.com",
    )
    db.add(husk)
    await db.flush()
    await db.commit()

    scrape_summary = await run_scrape_homepages(
        db, DeadZoneShellClient(), browser_client=_mock_browser()
    )
    assert scrape_summary.pages_via_browser_fallback == 1

    canned_judge = CompanyDescription(
        description_short="An AI answer engine with cited answers.",
        primary_category="ai search",
        tags=["ai", "search"],
        website_state="ok",
    )
    canned_long = CompanyLongDescription(
        description_long="Paragraph one.\n\nParagraph two."
    )

    async def fake_complete_json(prompt: str, schema: type, **kwargs: object) -> object:
        if schema is CompanyDescription:
            return canned_judge
        assert schema is CompanyLongDescription, f"unexpected schema {schema}"
        return canned_long

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json", fake_complete_json
    )

    enrich_summary = await run_enrich_companies(db)

    assert enrich_summary.companies_enriched == 1
    assert enrich_summary.descriptions_written == 1
    await db.refresh(husk)
    assert husk.description_short == "An AI answer engine with cited answers."
    assert husk.description_long == "Paragraph one.\n\nParagraph two."
