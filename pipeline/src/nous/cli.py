import logging

import click


@click.group()
def cli() -> None:
    """nous pipeline CLI."""
    # Configure the root logger once for the entire CLI so every stage's
    # logger.info() lines (including run telemetry) are visible in CI logs.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@cli.command("resolve-homepages")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of companies to process (for testing / partial runs).",
)
@click.option(
    "--refetch-after-days",
    type=int,
    default=90,
    show_default=True,
    help="Re-attempt resolution for companies last checked more than N days ago.",
)
@click.option(
    "--max-runtime-minutes",
    type=float,
    default=None,
    help=(
        "Wall-clock budget: stop cleanly at the next company boundary once "
        "exceeded. Remaining companies are picked up by the next run."
    ),
)
def resolve_homepages(
    limit: int | None, refetch_after_days: int, max_runtime_minutes: float | None
) -> None:
    """Attempt to resolve a homepage URL for companies that lack one."""
    import asyncio

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.resolve_homepages import run_resolve_homepages
    from nous.sources.homepage import HomepageClient

    settings = Settings()

    async def _run() -> None:
        async with (
            HomepageClient(
                settings.SEC_USER_AGENT,
                requests_per_second_per_domain=1.0,
            ) as homepage_client,
            AsyncSessionLocal() as session,
        ):
            summary = await run_resolve_homepages(
                session,
                homepage_client,
                refetch_after_days=refetch_after_days,
                limit=limit,
                max_runtime_minutes=max_runtime_minutes,
            )
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("scrape-homepages")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of companies to process.",
)
@click.option(
    "--refetch-after-days",
    type=int,
    default=90,
    show_default=True,
    help="Re-scrape companies whose pages are older than N days.",
)
@click.option(
    "--no-browser-fallback",
    is_flag=True,
    default=False,
    help="Disable the headless-Chromium fallback for JS-shell pages.",
)
@click.option(
    "--max-runtime-minutes",
    type=float,
    default=None,
    help=(
        "Wall-clock budget: stop cleanly at the next company boundary once "
        "exceeded. Remaining companies are picked up by the next run."
    ),
)
def scrape_homepages(
    limit: int | None,
    refetch_after_days: int,
    no_browser_fallback: bool,
    max_runtime_minutes: float | None,
) -> None:
    """Fetch each company's homepage and store raw HTML in raw_pages."""
    import asyncio

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.scrape_homepages import run_scrape_homepages
    from nous.sources.headless_browser import HeadlessBrowserClient
    from nous.sources.homepage import HomepageClient

    settings = Settings()

    async def _run() -> None:
        async with (
            HomepageClient(
                settings.SEC_USER_AGENT,
                requests_per_second_per_domain=1.0,
            ) as homepage_client,
            AsyncSessionLocal() as session,
        ):
            browser_client: HeadlessBrowserClient | None = None
            if not no_browser_fallback:
                browser_client = HeadlessBrowserClient(user_agent=settings.SEC_USER_AGENT)
                await browser_client.__aenter__()
            try:
                summary = await run_scrape_homepages(
                    session,
                    homepage_client,
                    refetch_after_days=refetch_after_days,
                    limit=limit,
                    browser_client=browser_client,
                    max_runtime_minutes=max_runtime_minutes,
                )
                click.echo(summary.model_dump_json(indent=2))
            finally:
                if browser_client is not None:
                    await browser_client.__aexit__(None, None, None)

    asyncio.run(_run())


@cli.command("enrich-companies")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of companies to enrich (for testing / quota management).",
)
@click.option(
    "--refetch-after-days",
    type=int,
    default=None,
    help=(
        "Force re-enrichment of companies enriched more than N days ago. "
        "Default: write-once (only enrich companies missing a description or "
        "people). Description + people are stable data, not refreshed weekly."
    ),
)
def enrich_companies(limit: int | None, refetch_after_days: int | None) -> None:
    """Call the LLM to generate descriptions + people for companies with raw pages."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry
    from nous.pipeline.enrich_companies import run_enrich_companies

    async def _run() -> None:
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_enrich_companies(
                    session,
                    max_companies=limit,
                    refetch_after_days=refetch_after_days,
                )
                click.echo(summary.model_dump_json(indent=2))
        finally:
            emit_run_telemetry("enrich-companies")

    asyncio.run(_run())


@cli.command("refresh-vc-portfolios")
@click.option(
    "--firm",
    "firms",
    multiple=True,
    default=(),
    help=(
        "Restrict the run to one or more firm slugs (matches keys in "
        "nous.sources.vc_portfolios.ADAPTERS, e.g. 'yc', 'a16z'). "
        "Repeatable. Default: run every registered adapter."
    ),
)
@click.option(
    "--similarity-threshold",
    type=float,
    default=None,
    help=(
        "pg_trgm similarity threshold for fuzzy company-name matching. "
        "Default: Settings.COMPANY_FUZZY_MATCH_THRESHOLD."
    ),
)
def refresh_vc_portfolios_cmd(
    firms: tuple[str, ...], similarity_threshold: float | None
) -> None:
    """Refresh companies from registered VC firm portfolio pages."""
    import asyncio

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.refresh_vc_portfolios import run_refresh_vc_portfolios
    from nous.sources.homepage import HomepageClient

    settings = Settings()
    threshold = (
        similarity_threshold
        if similarity_threshold is not None
        else settings.COMPANY_FUZZY_MATCH_THRESHOLD
    )
    firms_list: list[str] | None = list(firms) if firms else None

    logger = logging.getLogger("nous.cli.refresh_vc_portfolios")

    async def _run() -> None:
        async with (
            HomepageClient(
                settings.SEC_USER_AGENT,
                requests_per_second_per_domain=1.0,
            ) as homepage_client,
            AsyncSessionLocal() as session,
        ):
            summary = await run_refresh_vc_portfolios(
                session,
                homepage_client,
                firms=firms_list,
                similarity_threshold=threshold,
            )
            logger.info(
                "refresh-vc-portfolios summary: %s", summary.model_dump_json()
            )
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("ingest-news")
@click.option(
    "--lookback-days",
    type=int,
    default=7,
    show_default=True,
    help="Lookback window for Google News + TC venture feed.",
)
@click.option(
    "--no-techcrunch",
    is_flag=True,
    default=False,
    help="Skip the TechCrunch venture-tag broad sweep.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of companies to query (per-company Google News path).",
)
@click.option(
    "--similarity-threshold",
    type=float,
    default=None,
    help=(
        "pg_trgm similarity threshold for fuzzy company-name matching on the "
        "TC auto-create path. Default: Settings.COMPANY_FUZZY_MATCH_THRESHOLD."
    ),
)
def ingest_news(
    lookback_days: int,
    no_techcrunch: bool,
    limit: int | None,
    similarity_threshold: float | None,
) -> None:
    """Pull funding-keyword news articles into the news_articles table."""
    import asyncio

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry
    from nous.pipeline.ingest_news import run_ingest_news
    from nous.sources.news import NewsClient

    settings = Settings()
    threshold = (
        similarity_threshold
        if similarity_threshold is not None
        else settings.COMPANY_FUZZY_MATCH_THRESHOLD
    )

    async def _run() -> None:
        try:
            async with (
                NewsClient(
                    settings.SEC_USER_AGENT,
                    requests_per_second_per_domain=1.0,
                ) as news_client,
                AsyncSessionLocal() as session,
            ):
                summary = await run_ingest_news(
                    session,
                    news_client,
                    lookback_days=lookback_days,
                    include_techcrunch_broad=not no_techcrunch,
                    max_companies=limit,
                    similarity_threshold=threshold,
                )
                click.echo(summary.model_dump_json(indent=2))
        finally:
            emit_run_telemetry("ingest-news")

    asyncio.run(_run())


@cli.command("extract-funding")
@click.option(
    "--limit",
    type=int,
    default=1000,
    show_default=True,
    help="Maximum number of articles to process per run (weekly LLM budget).",
)
@click.option(
    "--include-low-confidence",
    is_flag=True,
    default=False,
    help="Persist rounds the LLM tagged as low-confidence (default: skip).",
)
def extract_funding(limit: int, include_low_confidence: bool) -> None:
    """Run the funding-extraction LLM over unprocessed news_articles."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry
    from nous.pipeline.extract_funding import run_extract_funding

    async def _run() -> None:
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_extract_funding(
                    session,
                    limit=limit,
                    skip_low_confidence=not include_low_confidence,
                )
                click.echo(summary.model_dump_json(indent=2))
        finally:
            emit_run_telemetry("extract-funding")

    asyncio.run(_run())


@cli.command("extract-funding-website")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of companies to process (for testing / quota management).",
)
@click.option(
    "--include-low-confidence",
    is_flag=True,
    default=False,
    help="Persist rounds the LLM tagged as low-confidence (default: skip).",
)
@click.option(
    "--recheck-after-days",
    type=int,
    default=180,
    show_default=True,
    help=(
        "Re-attempt companies whose website-funding pass ran more than N days "
        "ago. Attempts are stamped even when no funding is found, so bounded "
        "runs rotate through the backlog instead of re-processing the head."
    ),
)
def extract_funding_website(
    limit: int | None, include_low_confidence: bool, recheck_after_days: int
) -> None:
    """Gap-fill funding from a company's own website (fallback to TechCrunch).

    Runs only for companies that have scraped pages but no funding rounds yet,
    so the news/TechCrunch path always stays the primary source.
    """
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry
    from nous.pipeline.extract_funding import run_extract_funding_website

    async def _run() -> None:
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_extract_funding_website(
                    session,
                    limit=limit,
                    skip_low_confidence=not include_low_confidence,
                    recheck_after_days=recheck_after_days,
                )
                click.echo(summary.model_dump_json(indent=2))
        finally:
            emit_run_telemetry("extract-funding-website")

    asyncio.run(_run())


@cli.command("analyze-competitors")
@click.option(
    "--limit",
    type=int,
    default=500,
    show_default=True,
    help="Maximum number of companies to analyze per run (monthly LLM budget).",
)
@click.option(
    "--ttl-days",
    type=int,
    default=25,
    show_default=True,
    help="Skip companies whose competitors were updated within this many days.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run eligibility + LLM calls but skip the DB write.",
)
def analyze_competitors(limit: int, ttl_days: int, dry_run: bool) -> None:
    """Run the competitor-analysis LLM over eligible companies."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry
    from nous.pipeline.analyze_competitors import run_analyze_competitors

    async def _run() -> None:
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_analyze_competitors(
                    session,
                    limit=limit,
                    ttl_days=ttl_days,
                    dry_run=dry_run,
                )
                click.echo(summary.model_dump_json(indent=2))
        finally:
            emit_run_telemetry("analyze-competitors")

    asyncio.run(_run())


@cli.command("dedup-companies")
@click.option(
    "--llm-limit",
    type=int,
    default=200,
    show_default=True,
    help="Maximum LLM judgments for the fuzzy pass per run (highest-similarity first).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run reads + LLM calls but skip the merges/commits.",
)
def dedup_companies(llm_limit: int, dry_run: bool) -> None:
    """Collapse duplicate company rows (exact-domain, then LLM-gated fuzzy)."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry
    from nous.pipeline.dedup_companies import run_dedup_companies

    async def _run() -> None:
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_dedup_companies(
                    session,
                    llm_limit=llm_limit,
                    dry_run=dry_run,
                )
                click.echo(summary.model_dump_json(indent=2))
        finally:
            emit_run_telemetry("dedup-companies")

    asyncio.run(_run())


@cli.command("estimate-employees")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of companies to process (for testing / partial runs).",
)
@click.option(
    "--refetch-after-days",
    type=int,
    default=None,
    help=(
        "Re-estimate companies last checked more than N days ago. "
        "Default: the EMPLOYEE_REFETCH_DAYS setting (90)."
    ),
)
@click.option(
    "--max-runtime-minutes",
    type=float,
    default=None,
    help=(
        "Wall-clock budget: stop cleanly at the next company boundary once "
        "exceeded. Remaining companies are picked up by the next run."
    ),
)
def estimate_employees(
    limit: int | None,
    refetch_after_days: int | None,
    max_runtime_minutes: float | None,
) -> None:
    """Estimate employee headcount from public sources.

    Sources are tried in priority order (The Org, GrowJo, careers-page job
    count, GitHub org, Wellfound); the first non-null result wins and its
    source is recorded for attribution. Wellfound is tried last because it is
    mostly Cloudflare-blocked.
    """
    import asyncio

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.estimate_employees import run_estimate_employees
    from nous.sources.homepage import HomepageClient

    settings = Settings()
    effective_refetch = (
        refetch_after_days
        if refetch_after_days is not None
        else settings.EMPLOYEE_REFETCH_DAYS
    )

    async def _run() -> None:
        async with (
            HomepageClient(
                settings.SEC_USER_AGENT,
                requests_per_second_per_domain=1.0,
            ) as homepage_client,
            AsyncSessionLocal() as session,
        ):
            summary = await run_estimate_employees(
                session,
                homepage_client,
                settings.GITHUB_TOKEN,
                refetch_after_days=effective_refetch,
                limit=limit,
                max_runtime_minutes=max_runtime_minutes,
            )
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("snapshot-companies")
@click.option(
    "--week",
    type=str,
    default=None,
    help=(
        "Capture week as YYYY-MM-DD (normalized to that ISO week's Monday). "
        "Default: the current ISO week's Monday. Use for backfill."
    ),
)
def snapshot_companies(week: str | None) -> None:
    """Snapshot every company's headcount + trailing-30-day news count.

    One set-based upsert into company_snapshots, keyed by (company, ISO-week
    Monday). Idempotent: re-running for the same week refreshes the row in
    place. Cheap enough to run weekly; backfill a past week with --week.
    """
    import asyncio
    from datetime import date as _date

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.snapshot_companies import run_snapshot_companies

    parsed_week: _date | None = (
        _date.fromisoformat(week) if week is not None else None
    )

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            summary = await run_snapshot_companies(session, week=parsed_week)
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("db-stats")
@click.option(
    "--cap-mb",
    type=int,
    default=None,
    help="Database size cap in MB. Default: Settings.DB_SIZE_CAP_MB (500).",
)
@click.option(
    "--warn-pct",
    type=int,
    default=None,
    help="Warn threshold as a percentage of the cap. Default: Settings.DB_SIZE_WARN_PCT (80).",
)
def db_stats(cap_mb: int | None, warn_pct: int | None) -> None:
    """Report per-table and total database sizes; warn if nearing the free-tier cap."""
    import asyncio

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.db_stats import emit_db_stats_summary, run_db_stats

    settings = Settings()
    effective_cap = cap_mb if cap_mb is not None else settings.DB_SIZE_CAP_MB
    effective_warn = warn_pct if warn_pct is not None else settings.DB_SIZE_WARN_PCT

    _logger = logging.getLogger("nous.cli.db_stats")

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            summary = await run_db_stats(
                session,
                cap_mb=effective_cap,
                warn_pct=effective_warn,
            )
        click.echo(summary.model_dump_json(indent=2))
        emit_db_stats_summary(summary)
        if summary.warn:
            _logger.warning(
                "DB SIZE WARNING: %.1f MB used of %d MB cap (%.1f%%) — "
                "approaching Supabase free-tier limit",
                summary.total_bytes / (1024 * 1024),
                effective_cap,
                summary.pct_of_cap,
            )

    asyncio.run(_run())


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
