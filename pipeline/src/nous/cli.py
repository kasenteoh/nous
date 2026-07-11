import logging
from pathlib import Path

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
        "Wall-clock budget: stop cleanly at the next batch boundary once "
        "exceeded. Remaining companies are picked up by the next run."
    ),
)
@click.option(
    "--concurrency",
    type=int,
    default=8,
    show_default=True,
    help=(
        "How many companies to resolve over the network at once. Only HTTP is "
        "parallelized; DB writes stay sequential. Distinct companies use "
        "distinct domains, so the 1 req/sec/domain budget is preserved."
    ),
)
def resolve_homepages(
    limit: int | None,
    refetch_after_days: int,
    max_runtime_minutes: float | None,
    concurrency: int,
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
                concurrency=concurrency,
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
        "Wall-clock budget: stop cleanly at the next batch boundary once "
        "exceeded. Remaining companies are picked up by the next run."
    ),
)
@click.option(
    "--concurrency",
    type=int,
    default=6,
    show_default=True,
    help=(
        "How many companies to scrape over the network at once. Only HTTP is "
        "parallelized; page persistence stays sequential on one session. A "
        "single company's own pages stay serial (same host)."
    ),
)
def scrape_homepages(
    limit: int | None,
    refetch_after_days: int,
    no_browser_fallback: bool,
    max_runtime_minutes: float | None,
    concurrency: int,
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
                    concurrency=concurrency,
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
@click.option(
    "--backfill-missing-taxonomy",
    is_flag=True,
    default=False,
    help=(
        "Select companies that have description_long but NULL industry_group "
        "(and/or primary_category) and re-run enrichment to populate the missing "
        "taxonomy fields. These companies are otherwise ineligible for "
        "analyze-competitors. Idempotent: once industry_group is set the company "
        "drops out of the selection. Mutually exclusive with --refetch-after-days."
    ),
)
@click.option(
    "--redescribe-outdated",
    is_flag=True,
    default=False,
    help=(
        "After the normal pass, additionally regenerate description_long for "
        "companies whose enrichment_prompt_version is NULL or older than the "
        "current description prompt (shown companies with scraped content "
        "only). Descriptions only — eligibility/people/taxonomy untouched. "
        "Bounded by --limit; idempotent (each visited company is stamped "
        "current and drops out). Mutually exclusive with "
        "--backfill-missing-taxonomy."
    ),
)
def enrich_companies(
    limit: int | None,
    refetch_after_days: int | None,
    backfill_missing_taxonomy: bool,
    redescribe_outdated: bool,
) -> None:
    """Call the LLM to generate descriptions + people for companies with raw pages."""
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry, record_pipeline_run
    from nous.pipeline.enrich_companies import (
        run_enrich_companies,
        run_redescribe_outdated,
    )

    if redescribe_outdated and backfill_missing_taxonomy:
        raise click.UsageError(
            "--redescribe-outdated and --backfill-missing-taxonomy are "
            "mutually exclusive (one regenerates descriptions, the other "
            "taxonomy)."
        )

    async def _run() -> None:
        started = datetime.now(UTC)
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_enrich_companies(
                    session,
                    max_companies=limit,
                    refetch_after_days=refetch_after_days,
                    backfill_missing_taxonomy=backfill_missing_taxonomy,
                )
                click.echo(summary.model_dump_json(indent=2))
            await record_pipeline_run(
                "enrich-companies",
                started_at=started,
                inputs_seen=summary.companies_seen,
                rows_written=summary.companies_enriched,
                summary=summary,
                flag_empty=True,
            )
            if redescribe_outdated:
                # Second pass, separately bounded by --limit: drain the
                # outdated-description backlog. Recorded as its own
                # pipeline_runs row so the drain is observable; flag_empty
                # stays off because an empty selection is the steady state
                # once the backlog drains.
                redescribe_started = datetime.now(UTC)
                async with AsyncSessionLocal() as session:
                    redescribe_summary = await run_redescribe_outdated(
                        session, max_companies=limit
                    )
                    click.echo(redescribe_summary.model_dump_json(indent=2))
                await record_pipeline_run(
                    "redescribe-outdated",
                    started_at=redescribe_started,
                    inputs_seen=redescribe_summary.companies_seen,
                    rows_written=redescribe_summary.descriptions_written,
                    summary=redescribe_summary,
                )
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
    from datetime import UTC, datetime

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry, record_pipeline_run
    from nous.pipeline.ingest_news import run_ingest_news
    from nous.sources.news import NewsClient

    settings = Settings()
    threshold = (
        similarity_threshold
        if similarity_threshold is not None
        else settings.COMPANY_FUZZY_MATCH_THRESHOLD
    )

    async def _run() -> None:
        started = datetime.now(UTC)
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
            await record_pipeline_run(
                "ingest-news",
                started_at=started,
                inputs_seen=summary.articles_seen,
                rows_written=summary.articles_inserted + summary.auto_created_companies,
                summary=summary,
                # flag_empty=True: a run that queried companies but wrote nothing
                # is classified status='empty' so pipeline-health surfaces it as
                # a regression signal (this silent-failure mode hid the news
                # coverage bug for months — see Task 4.1 diagnosis).
                flag_empty=True,
            )
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
@click.option(
    "--requery-totals",
    is_flag=True,
    default=False,
    help=(
        "One-time backfill: instead of unprocessed articles, re-run "
        "ALREADY-processed articles whose text mentions a cumulative total "
        "('to date', 'total funding', ...) and whose company has no stated "
        "total yet, capped by --limit. Idempotent — rounds reconcile, status "
        "never downgrades, totals apply newest-wins, articles stay processed."
    ),
)
def extract_funding(
    limit: int, include_low_confidence: bool, requery_totals: bool
) -> None:
    """Run the funding-extraction LLM over unprocessed news_articles."""
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry, record_pipeline_run
    from nous.pipeline.extract_funding import run_extract_funding

    async def _run() -> None:
        started = datetime.now(UTC)
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_extract_funding(
                    session,
                    limit=limit,
                    skip_low_confidence=not include_low_confidence,
                    requery_totals=requery_totals,
                )
                click.echo(summary.model_dump_json(indent=2))
            await record_pipeline_run(
                "extract-funding",
                started_at=started,
                inputs_seen=summary.articles_processed,
                rows_written=summary.funding_rounds_created
                + summary.funding_rounds_merged,
                summary=summary,
            )
        finally:
            emit_run_telemetry("extract-funding")

    asyncio.run(_run())


# ~5-year window: long enough to recover a company's full visible funding
# trajectory (Seed → Series A → B → ...) from historical news. The reconcile
# key (round_type + announced_date ±60d, both-null → insert) makes the long
# sweep dup-safe, so re-runs don't duplicate (Task A3).
_BACKFILL_LOOKBACK_DAYS = 1825


@cli.command("backfill-funding-history")
@click.option(
    "--news-limit",
    type=int,
    default=400,
    show_default=True,
    help=(
        "Max funded/notable companies to sweep for historical news this run "
        "(per-company Google News, least-recently-checked first). Bounds the "
        "backfill's DeepSeek + scraping spend."
    ),
)
@click.option(
    "--funding-limit",
    type=int,
    default=400,
    show_default=True,
    help="Max news_articles to run funding-extraction over after the sweep.",
)
@click.option(
    "--lookback-days",
    type=int,
    default=_BACKFILL_LOOKBACK_DAYS,
    show_default=True,
    help="Historical lookback window for the Google News sweep (~5y default).",
)
@click.option(
    "--include-low-confidence",
    is_flag=True,
    default=False,
    help="Persist rounds the LLM tagged as low-confidence (default: skip).",
)
def backfill_funding_history(
    news_limit: int,
    funding_limit: int,
    lookback_days: int,
    include_low_confidence: bool,
) -> None:
    """Backfill multi-round funding HISTORIES for funded/notable companies.

    The standing ingest-news lookback (14d) only ever captures a company's
    latest round, so no trajectories exist (Task A3). This command sweeps a
    long (~5y) Google News window over companies that already have a round OR
    existing news coverage, then runs funding-extraction over the new articles.
    It leans entirely on reconcile_funding_round for dedup — distinct rounds
    (round_type + announced_date) insert separately; re-runs merge, never
    duplicate. The TC broad sweep is intentionally OFF (this targets existing
    companies' depth, not discovery of new ones).
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry, record_pipeline_run
    from nous.pipeline.extract_funding import run_extract_funding
    from nous.pipeline.ingest_news import run_ingest_news
    from nous.sources.news import NewsClient

    settings = Settings()

    async def _run() -> None:
        ingest_started = datetime.now(UTC)
        try:
            async with (
                NewsClient(
                    settings.SEC_USER_AGENT,
                    requests_per_second_per_domain=1.0,
                ) as news_client,
                AsyncSessionLocal() as session,
            ):
                ingest_summary = await run_ingest_news(
                    session,
                    news_client,
                    lookback_days=lookback_days,
                    include_techcrunch_broad=False,
                    max_companies=news_limit,
                    funded_or_notable_only=True,
                )
                click.echo("ingest-news (backfill):")
                click.echo(ingest_summary.model_dump_json(indent=2))
            await record_pipeline_run(
                "backfill-funding-history:ingest",
                started_at=ingest_started,
                inputs_seen=ingest_summary.articles_seen,
                rows_written=ingest_summary.articles_inserted,
                summary=ingest_summary,
            )
        finally:
            emit_run_telemetry("backfill-funding-history:ingest")

        extract_started = datetime.now(UTC)
        try:
            async with AsyncSessionLocal() as session:
                extract_summary = await run_extract_funding(
                    session,
                    limit=funding_limit,
                    skip_low_confidence=not include_low_confidence,
                )
                click.echo("extract-funding (backfill):")
                click.echo(extract_summary.model_dump_json(indent=2))
            await record_pipeline_run(
                "backfill-funding-history:extract",
                started_at=extract_started,
                inputs_seen=extract_summary.articles_processed,
                rows_written=extract_summary.funding_rounds_created
                + extract_summary.funding_rounds_merged,
                summary=extract_summary,
            )
        finally:
            emit_run_telemetry("backfill-funding-history:extract")

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
@click.option(
    "--ignore-recheck",
    is_flag=True,
    default=False,
    help=(
        "One-time drain (Task A2): ignore the recheck back-off and mine EVERY "
        "round-less company's own site once, regardless of when it was last "
        "checked. Pair with a high --limit. Idempotent — reconcile dedups."
    ),
)
@click.option(
    "--concurrency",
    type=int,
    default=5,
    show_default=True,
    help=(
        "How many companies' website-funding LLM calls to run at once. Only "
        "the LLM call is parallelized; DB reads/writes stay sequential on one "
        "session. A 429 stops scheduling further work at a batch boundary."
    ),
)
def extract_funding_website(
    limit: int | None,
    include_low_confidence: bool,
    recheck_after_days: int,
    ignore_recheck: bool,
    concurrency: int,
) -> None:
    """Gap-fill funding from a company's own website (fallback to TechCrunch).

    Runs only for companies that have scraped pages but no funding rounds yet,
    so the news/TechCrunch path always stays the primary source.
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry, record_pipeline_run
    from nous.pipeline.extract_funding import run_extract_funding_website

    async def _run() -> None:
        started = datetime.now(UTC)
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_extract_funding_website(
                    session,
                    limit=limit,
                    skip_low_confidence=not include_low_confidence,
                    recheck_after_days=recheck_after_days,
                    ignore_recheck=ignore_recheck,
                    concurrency=concurrency,
                )
                click.echo(summary.model_dump_json(indent=2))
            await record_pipeline_run(
                "extract-funding-website",
                started_at=started,
                inputs_seen=summary.companies_seen,
                rows_written=summary.funding_rounds_created
                + summary.funding_rounds_merged,
                summary=summary,
                # flag_empty=True: a run that inspected companies but wrote 0
                # funding rows is classified status='empty' so pipeline-health
                # surfaces it — the same silent-failure mode that masked the
                # news coverage gap for months (Task 4.1 diagnosis). Without
                # record_pipeline_run here, a zero-output website run was
                # entirely invisible to the observability layer.
                flag_empty=True,
            )
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
@click.option(
    "--concurrency",
    type=int,
    default=5,
    show_default=True,
    help=(
        "How many companies' LLM passes to run at once (2 DeepSeek calls each). "
        "Only the LLM work is parallelized; DB reads/writes stay sequential on "
        "one session. A 429 stops scheduling further work at a batch boundary."
    ),
)
def analyze_competitors(
    limit: int, ttl_days: int, dry_run: bool, concurrency: int
) -> None:
    """Run the competitor-analysis LLM over eligible companies."""
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry, record_pipeline_run
    from nous.pipeline.analyze_competitors import run_analyze_competitors

    async def _run() -> None:
        started = datetime.now(UTC)
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_analyze_competitors(
                    session,
                    limit=limit,
                    ttl_days=ttl_days,
                    dry_run=dry_run,
                    concurrency=concurrency,
                )
                click.echo(summary.model_dump_json(indent=2))
            if not dry_run:
                await record_pipeline_run(
                    "analyze-competitors",
                    started_at=started,
                    inputs_seen=summary.companies_analyzed,
                    rows_written=summary.competitors_written,
                    summary=summary,
                    flag_empty=True,
                )
        finally:
            emit_run_telemetry("analyze-competitors")

    asyncio.run(_run())


@cli.command("refresh-investor-counts")
def refresh_investor_counts_cmd() -> None:
    """Recompute investors.portfolio_count across both link tables.

    Counts distinct non-excluded companies per investor via company_investors
    and funding_round_investors → funding_rounds (UNION, so no double-count).
    Idempotent: a full recompute from first principles, including zeroing
    investors with no qualifying links.
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run
    from nous.pipeline.refresh_investor_counts import refresh_investor_counts

    async def _run() -> None:
        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await refresh_investor_counts(session)
            await session.commit()
            click.echo(summary.model_dump_json(indent=2))
        await record_pipeline_run(
            "refresh-investor-counts",
            started_at=started,
            inputs_seen=0,
            rows_written=summary.investors_updated,
            summary=summary,
        )

    asyncio.run(_run())


@cli.command("refresh-latest-round")
def refresh_latest_round_cmd() -> None:
    """Recompute the denormalized latest_round_* columns on companies.

    Flattens each company's most-recent funding round (greatest announced_date,
    NULLS LAST) onto companies.latest_round_amount / latest_round_date /
    latest_round_type so the web browse page can sort/filter without a
    cross-table aggregate. Set-based and idempotent: a full recompute that also
    clears stale values for companies whose last round was removed.
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run
    from nous.pipeline.refresh_latest_round import refresh_latest_round

    async def _run() -> None:
        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await refresh_latest_round(session)
            await session.commit()
            click.echo(summary.model_dump_json(indent=2))
        await record_pipeline_run(
            "refresh-latest-round",
            started_at=started,
            inputs_seen=0,
            rows_written=summary.companies_with_round,
            summary=summary,
        )

    asyncio.run(_run())


@cli.command("dedup-investors")
def dedup_investors_cmd() -> None:
    """Purge junk investor rows, collapse duplicates, then classify type.

    First deletes non-investor placeholder rows ("a group of investors",
    "undisclosed", "angel investors", …) and their noise links. Then groups the
    rest by post-alias canonical name, picks the survivor (most links → oldest
    created_at), and merges losers via merge_investors (which repoints
    company_investors + funding_round_investors and calls
    refresh-investor-counts). Finally sets type='institutional' for known VC
    firms and type='angel' for individual-looking names.

    Idempotent: a second run finds no junk/duplicates and reclassifies to the
    same types, so it is a no-op.
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run
    from nous.pipeline.dedup_investors import run_dedup_investors

    async def _run() -> None:
        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await run_dedup_investors(session)
            click.echo(summary.model_dump_json(indent=2))
        await record_pipeline_run(
            "dedup-investors",
            started_at=started,
            inputs_seen=summary.investors_seen,
            rows_written=(
                summary.junk_purged
                + summary.investors_merged
                + summary.type_classifications
                + summary.angel_classifications
            ),
            summary=summary,
        )

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


@cli.command("normalize-taxonomy")
def normalize_taxonomy_cmd() -> None:
    """Recanonicalize companies' free-text taxonomy in place (zero LLM).

    Applies nous.util.category.normalize_category to primary_category,
    nous.util.industry.normalize_industry to industry_group, AND
    nous.util.tags.canonicalize_tags to tags, collapsing the historical
    free-text spelling sprawl (ad-tech / adtech / advertising technology;
    biotech / biotech tooling; healthcare / healthtech / healthcare AI;
    ci-observability / ci-cd) onto the canonical sets. Backfilling here heals
    the browse dropdown and the /tag/* long tail without waiting on
    re-enrichment. Set-based per distinct value (tags: per distinct array)
    and idempotent: a second run finds nothing to change. No schema change
    (content update only).
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run
    from nous.pipeline.normalize_taxonomy import run_normalize_taxonomy

    async def _run() -> None:
        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await run_normalize_taxonomy(session)
            click.echo(summary.model_dump_json(indent=2))
        await record_pipeline_run(
            "normalize-taxonomy",
            started_at=started,
            inputs_seen=summary.distinct_values_seen,
            rows_written=summary.rows_updated,
            summary=summary,
        )

    asyncio.run(_run())


@cli.command("name-quality")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of companies to consider (for testing / partial runs).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log intended casing upgrades without writing.",
)
def name_quality_cmd(limit: int | None, dry_run: bool) -> None:
    """Improve company display-name CASING from the stored homepage title (zero LLM).

    Reads each company's homepage RawPage (whose stored content begins with the
    page <title> / og meta that scrape-homepages prepends) and upgrades
    company.name to the better-cased brand ONLY when the candidate is an
    unambiguous pure-casing variant of the current name — same normalized_name,
    only the letter case differs, and the current casing is degenerate
    (all-lowercase or all-uppercase). e.g. "docusign" -> "DocuSign". Never
    changes a name to a different word, never touches slug/normalized_name.
    Idempotent: a second run finds nothing to upgrade. A safe no-op when the
    stored content carries no usable title/brand line.
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run
    from nous.pipeline.name_quality import run_name_quality

    async def _run() -> None:
        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await run_name_quality(session, limit=limit, dry_run=dry_run)
            click.echo(summary.model_dump_json(indent=2))
        if not dry_run:
            await record_pipeline_run(
                "name-quality",
                started_at=started,
                inputs_seen=summary.companies_seen,
                rows_written=summary.names_upgraded,
                summary=summary,
            )

    asyncio.run(_run())


@cli.command("link-competitors")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Max dangling competitor rows to attempt (for testing / partial runs).",
)
@click.option(
    "--threshold",
    type=float,
    default=0.45,
    show_default=True,
    help="Minimum pg_trgm similarity to accept a fuzzy company match.",
)
@click.option(
    "--tie-margin",
    type=float,
    default=0.08,
    show_default=True,
    help="Skip a match when the runner-up is within this similarity of the best.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what would be linked without writing.",
)
def link_competitors(
    limit: int | None, threshold: float, tie_margin: float, dry_run: bool
) -> None:
    """Fuzzy-resolve dangling competitors.competitor_company_id FKs (zero LLM).

    analyze-competitors only links competitor names that match a company's
    normalized_name exactly; this densifies the graph by trigram-matching the
    rest, best-match-only with a tie guard. Idempotent (only touches NULL FKs).
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run
    from nous.pipeline.link_competitors import run_link_competitors

    async def _run() -> None:
        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await run_link_competitors(
                session,
                limit=limit,
                threshold=threshold,
                tie_margin=tie_margin,
                dry_run=dry_run,
            )
            click.echo(summary.model_dump_json(indent=2))
        if not dry_run:
            await record_pipeline_run(
                "link-competitors",
                started_at=started,
                inputs_seen=summary.rows_seen,
                rows_written=summary.linked,
                summary=summary,
            )

    asyncio.run(_run())


@cli.command("derive-relationships")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Compute and report edge counts without writing.",
)
@click.option(
    "--max-similar-per-company",
    type=int,
    default=8,
    show_default=True,
    help="Cap on 'similar' edges derived per company.",
)
def derive_relationships(dry_run: bool, max_similar_per_company: int) -> None:
    """Rebuild the company_relationships graph from competitors + industry/tags.

    Replace-style and idempotent, zero LLM. Run after link-competitors so the
    competitor projection picks up freshly resolved FKs.
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run
    from nous.pipeline.derive_relationships import run_derive_relationships

    async def _run() -> None:
        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await run_derive_relationships(
                session,
                dry_run=dry_run,
                max_similar_per_company=max_similar_per_company,
            )
            click.echo(summary.model_dump_json(indent=2))
        if not dry_run:
            await record_pipeline_run(
                "derive-relationships",
                started_at=started,
                inputs_seen=summary.competitor_edges + summary.similar_edges,
                rows_written=summary.competitor_edges + summary.similar_edges,
                summary=summary,
            )

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


@cli.command("judge-eligibility")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of companies to judge (caps LLM spend per run).",
)
@click.option(
    "--rejudge-nonstartup-signals",
    is_flag=True,
    default=False,
    help=(
        "ALSO re-judge currently-included companies whose stored description "
        "matches a clearly-non-startup prose signal (business directories, "
        "coaching/courses shops, agencies, decades-old businesses — the "
        "Manta/Lucra leak) under the tightened prompt. Resets their "
        "eligibility stamp so the normal path re-judges them; the LLM still "
        "makes the final call and already-excluded rows are left untouched. "
        "Off by default, so the production cron is behaviourally unchanged."
    ),
)
def judge_eligibility(limit: int | None, rejudge_nonstartup_signals: bool) -> None:
    """Backfill the is-this-a-startup judgment for already-enriched companies."""
    import asyncio

    from nous.db.session import get_session_factory
    from nous.observability import emit_run_telemetry
    from nous.pipeline.judge_eligibility import run_judge_eligibility

    async def _run() -> None:
        # The stage manages its own per-company sessions from the factory, so a
        # wedged free-tier connection skips one company instead of hanging.
        try:
            summary = await run_judge_eligibility(
                get_session_factory(),
                limit=limit,
                rejudge_nonstartup_signals=rejudge_nonstartup_signals,
            )
            click.echo(summary.model_dump_json(indent=2))
        finally:
            emit_run_telemetry("judge-eligibility")

    asyncio.run(_run())


@cli.command("infer-hq-country")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Max companies to check (caps fetches + LLM spend per run).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log intended exclusions/updates without writing.",
)
def infer_hq_country(limit: int | None, dry_run: bool) -> None:
    """Detect non-US HQ for shown companies with hq_country NULL.

    Fetches each company's own about/contact/legal pages and judges country
    from that text; soft-excludes non-US companies on positive sourced
    evidence, leaving genuinely-unknown US-plausible companies alone.
    """
    import asyncio

    from nous.config import Settings
    from nous.db.session import get_session_factory
    from nous.observability import emit_run_telemetry
    from nous.pipeline.infer_hq_country import run_infer_hq_country
    from nous.sources.homepage import HomepageClient

    settings = Settings()

    async def _run() -> None:
        try:
            async with HomepageClient(
                settings.SEC_USER_AGENT,
                requests_per_second_per_domain=1.0,
            ) as client:
                summary = await run_infer_hq_country(
                    get_session_factory(), client, limit=limit, dry_run=dry_run
                )
            click.echo(summary.model_dump_json(indent=2))
        finally:
            emit_run_telemetry("infer-hq-country")

    asyncio.run(_run())


@cli.command("pipeline-health")
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help=(
        "Exit non-zero when any stage logged status='empty' or status='error'. "
        "Default: always exit 0 (annotate only), matching continue-on-error semantics."
    ),
)
def pipeline_health(strict: bool) -> None:
    """Inspect pipeline_runs for empty/error stages and emit CI annotations.

    Queries the most-recent pipeline_runs row for every stage and prints a
    GitHub Actions ``::warning::`` / ``::error::`` annotation for each non-green
    stage.  Appends a markdown table to the step summary when GITHUB_STEP_SUMMARY
    is set.  Exits 0 by default so it never blocks the pipeline; pass --strict
    to exit non-zero on any non-green stage.
    """
    import asyncio
    import sys

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.pipeline_health import (
        HealthReport,
        emit_health_annotations,
        run_pipeline_health,
    )

    async def _run() -> HealthReport:
        async with AsyncSessionLocal() as session:
            return await run_pipeline_health(session)

    report = asyncio.run(_run())
    emit_health_annotations(report)

    if report.stages:
        click.echo(
            f"pipeline-health: checked {len(report.stages)} stage(s), "
            f"{len(report.bad)} non-green"
        )
    else:
        click.echo("pipeline-health: no pipeline_runs rows found (table is empty)")

    if strict and not report.all_green:
        sys.exit(1)


@cli.command("adapter-health")
@click.option(
    "--floor",
    type=int,
    default=None,
    help=(
        "Global minimum entry count an adapter must exceed to count as healthy. "
        "Per-firm overrides in nous.pipeline.adapter_health.ADAPTER_FLOORS take "
        "precedence. Default: adapter_health.DEFAULT_GLOBAL_FLOOR (10)."
    ),
)
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help=(
        "Exit non-zero when any adapter is at or below its floor (or raised). "
        "Default: always exit 0 (annotate only), matching pipeline-health and "
        "the pipeline's continue-on-error semantics. Use --strict for a "
        "dedicated alerting cron."
    ),
)
def adapter_health(floor: int | None, strict: bool) -> None:
    """Canary the discovery adapters: warn when any source's yield collapses.

    Runs every registered VC adapter in nous.sources.vc_portfolios.ADAPTERS
    against a live HomepageClient AND every broad funding-news feed in
    nous.pipeline.adapter_health.NEWS_FEEDS against a live NewsClient, counts
    the entries each yields, and compares the count to a floor (configurable
    global floor with per-firm overrides for VC adapters; feeds are reported
    as ``news:<slug>`` and are healthy on any entry at all). Any source at or
    below its floor — including one that raises — gets a GitHub Actions
    ``::warning::`` annotation, appears in the step-summary table, and is
    recorded in a single pipeline_runs audit row. Read-only otherwise; one
    broken source never aborts the others. Exits 0 by default; pass --strict
    to exit non-zero.
    """
    import sys

    from nous.config import Settings
    from nous.observability import emit_run_telemetry
    from nous.pipeline.adapter_health import (
        DEFAULT_GLOBAL_FLOOR,
        AdapterHealthReport,
        emit_adapter_health_annotations,
        run_adapter_health_sync,
    )

    settings = Settings()
    effective_floor = floor if floor is not None else DEFAULT_GLOBAL_FLOOR

    try:
        report: AdapterHealthReport = run_adapter_health_sync(
            user_agent=settings.SEC_USER_AGENT,
            global_floor=effective_floor,
        )
    finally:
        # No LLM calls in this stage, but emit telemetry for a uniform run
        # footer across stages (the ledger will simply read zeroes).
        emit_run_telemetry("adapter-health")

    emit_adapter_health_annotations(report)

    click.echo(
        f"adapter-health: checked {len(report.adapters)} adapter(s), "
        f"{len(report.unhealthy)} below floor"
    )

    if strict and not report.all_healthy:
        sys.exit(1)


@cli.command("repair-catalog")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log intended repairs without writing.",
)
def repair_catalog(dry_run: bool) -> None:
    """One-time catalog repair: Lightspeed badge-suffix names + parked-domain rows."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.repair_catalog import run_repair_catalog

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            summary = await run_repair_catalog(session, dry_run=dry_run)
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("repair-wrong-websites")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log intended repairs without writing.",
)
def repair_wrong_websites(dry_run: bool) -> None:
    """Repair rows poisoned by the pre-hardening homepage resolver.

    Three passes (all idempotent — a second run is a no-op):

    \b
    (a) company.website host is in the aggregator/directory reject set
        → append bad URL to rejected_urls, clear website + enrichment fields,
          drop stale raw_pages so resolve→scrape→enrich restart cleanly.

    (b) company.description_short contains for-sale / parked prose
        → same clear action as (a).

    (c) exclusion_reason IN ('not_a_startup','non_us') with exclusion_detail
        referencing "personal homepage" or a wrong-site phrase
        → clear exclusion + eligibility_checked_at so judge-eligibility
          re-judges from the corrected site.
    """
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.repair_wrong_websites import run_repair_wrong_websites

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            summary = await run_repair_wrong_websites(session, dry_run=dry_run)
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("repair-duplicate-rounds")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log intended collapses/deletes without writing.",
)
def repair_duplicate_rounds(dry_run: bool) -> None:
    """Collapse same-amount duplicate funding rounds + drop fully-empty rows.

    Repairs the duplicate funding_rounds left by the historical news backfill
    (one round re-reported from many articles, several with null round_type and
    no date — e.g. Helion's $465M Series G as 5 rows → an inflated $2.3B total).
    Groups each company's rounds by amount_raised and collapses rows with
    compatible round_types (equal or null) to one survivor, folding the losers'
    non-null fields in and repointing their investor links. Idempotent: a
    second run finds nothing to collapse.
    """
    import asyncio
    from datetime import UTC, datetime

    from nous.db.session import AsyncSessionLocal
    from nous.observability import record_pipeline_run
    from nous.pipeline.repair_duplicate_rounds import run_repair_duplicate_rounds

    async def _run() -> None:
        started = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            summary = await run_repair_duplicate_rounds(session, dry_run=dry_run)
            click.echo(summary.model_dump_json(indent=2))
        if not dry_run:
            await record_pipeline_run(
                "repair-duplicate-rounds",
                started_at=started,
                inputs_seen=summary.companies_seen,
                rows_written=summary.empty_rows_deleted
                + summary.duplicate_rows_merged,
                summary=summary,
            )

    asyncio.run(_run())


@cli.command("exclude-company")
@click.argument("slug")
@click.option(
    "--reason",
    type=click.Choice(["parse_artifact", "non_us", "not_a_startup", "manual"]),
    default="manual",
    show_default=True,
    help="Recorded exclusion reason.",
)
@click.option("--detail", type=str, default=None, help="Free-form audit note.")
@click.option(
    "--clear",
    is_flag=True,
    default=False,
    help="Re-include the company (clears the exclusion).",
)
def exclude_company(slug: str, reason: str, detail: str | None, clear: bool) -> None:
    """Manually exclude (or --clear) a company from the catalog by slug."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.exclude_company import run_exclude_company

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            result = await run_exclude_company(
                session, slug=slug, reason=reason, detail=detail, clear=clear
            )
            click.echo(result.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("unexclude-company")
@click.argument("slug")
def unexclude_company(slug: str) -> None:
    """Re-include a company by clearing its exclusion (alias for exclude-company --clear).

    Example: unexclude-company abnormal-security
    """
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.exclude_company import run_exclude_company

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            result = await run_exclude_company(session, slug=slug, clear=True)
            click.echo(result.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("eval-prompts")
@click.option(
    "--record",
    is_flag=True,
    default=False,
    help=(
        "Re-run every fixture input against live DeepSeek (paid, requires "
        "DEEPSEEK_API_KEY) and rewrite recorded.json files before scoring. "
        "Without this flag the command is fully offline."
    ),
)
@click.option(
    "--update-baseline",
    is_flag=True,
    default=False,
    help=(
        "Rewrite tests/golden/baseline.json floors from the current scores "
        "(rounded down). Review the diff before committing."
    ),
)
@click.option(
    "--prompt",
    "prompt_name",
    type=str,
    default=None,
    help="Only evaluate/record this prompt (e.g. 'funding_extraction').",
)
@click.option(
    "--golden-dir",
    type=click.Path(exists=False, file_okay=False, path_type=Path),
    default=None,
    help="Override the golden fixtures directory (default: pipeline/tests/golden).",
)
def eval_prompts(
    record: bool,
    update_baseline: bool,
    prompt_name: str | None,
    golden_dir: Path | None,
) -> None:
    """Score LLM prompts against the golden set; optionally re-record live.

    Offline by default (deterministic, free): replays committed recorded.json
    responses through the runtime parse/validate path and gates the metrics
    against tests/golden/baseline.json, printing a per-metric delta table.
    Exits non-zero when any gated metric falls below its floor.
    """
    import asyncio

    from nous.evals import (
        PROMPT_SPECS,
        PromptSpec,
        check_floors,
        evaluate_prompt,
        floors_from_report,
        get_spec,
        load_baseline,
        render_report,
        save_baseline,
    )
    from nous.evals.harness import GoldenFixtureError, default_golden_dir
    from nous.evals.record import MissingAPIKeyError, record_prompt

    directory = golden_dir if golden_dir is not None else default_golden_dir()
    specs: tuple[PromptSpec, ...]
    if prompt_name is not None:
        try:
            specs = (get_spec(prompt_name),)
        except KeyError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        specs = PROMPT_SPECS

    if record:

        async def _record_all() -> None:
            for spec in specs:
                summary = await record_prompt(spec, directory)
                click.echo(summary.model_dump_json(indent=2))

        try:
            asyncio.run(_record_all())
        except MissingAPIKeyError as exc:
            raise click.ClickException(str(exc)) from exc

    try:
        baseline = load_baseline(directory)
    except GoldenFixtureError:
        if not update_baseline:
            raise click.ClickException(
                f"No readable baseline.json under {directory} — run with"
                " --update-baseline to create one from current scores."
            ) from None
        baseline = {}

    failures: list[str] = []
    for spec in specs:
        try:
            report = evaluate_prompt(spec, directory)
        except GoldenFixtureError as exc:
            raise click.ClickException(str(exc)) from exc
        if update_baseline:
            baseline[spec.name] = floors_from_report(report)
        click.echo(render_report(report, baseline.get(spec.name)))
        click.echo("")
        failures.extend(check_floors(report, baseline.get(spec.name, {})))

    if update_baseline:
        save_baseline(directory, baseline)
        click.echo(f"baseline floors written to {directory / 'baseline.json'}")
    elif failures:
        for failure in failures:
            click.echo(f"FAIL {failure}")
        raise SystemExit(1)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
