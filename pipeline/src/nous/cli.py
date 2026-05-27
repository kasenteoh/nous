import click


@click.group()
def cli() -> None:
    """nous pipeline CLI."""


def _stub(stage: str) -> None:
    click.echo(f"{stage} not yet implemented")


@cli.command("ingest-filings")
@click.option(
    "--since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Start date (inclusive). Default: today - 7 - EDGAR_OVERLAP_DAYS.",
)
@click.option(
    "--until",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="End date (inclusive). Default: today.",
)
def ingest_filings_cmd(since: object, until: object) -> None:
    """Ingest SEC Form D filings into the database."""
    import asyncio
    from datetime import date, datetime, timedelta

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.ingest_filings import run_ingest_filings
    from nous.sources.edgar import EdgarClient

    settings = Settings()
    until_dt = until if isinstance(until, datetime) else None
    since_dt = since if isinstance(since, datetime) else None
    until_d: date = until_dt.date() if until_dt is not None else date.today()
    since_d: date = (
        since_dt.date()
        if since_dt is not None
        else (until_d - timedelta(days=7 + settings.EDGAR_OVERLAP_DAYS))
    )

    async def _run() -> None:
        async with (
            EdgarClient(
                settings.SEC_USER_AGENT,
                requests_per_second=settings.EDGAR_REQUESTS_PER_SECOND,
            ) as edgar,
            AsyncSessionLocal() as session,
        ):
            summary = await run_ingest_filings(
                session,
                edgar,
                industry_groups=set(settings.INDUSTRY_GROUPS),
                since=since_d,
                until=until_d,
            )
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())


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
def resolve_homepages(limit: int | None, refetch_after_days: int) -> None:
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
    "--max-pages-per-company",
    type=int,
    default=4,
    show_default=True,
    help="Maximum number of pages to fetch per company.",
)
def scrape_homepages(
    limit: int | None,
    refetch_after_days: int,
    max_pages_per_company: int,
) -> None:
    """Fetch homepage + subpages and store raw HTML in raw_pages."""
    import asyncio

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.scrape_homepages import run_scrape_homepages
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
            summary = await run_scrape_homepages(
                session,
                homepage_client,
                refetch_after_days=refetch_after_days,
                limit=limit,
                max_pages_per_company=max_pages_per_company,
            )
            click.echo(summary.model_dump_json(indent=2))

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
    default=90,
    show_default=True,
    help="Re-enrich companies enriched more than N days ago.",
)
def enrich_companies(limit: int | None, refetch_after_days: int) -> None:
    """Call Gemini to generate descriptions for companies with raw pages."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.enrich_companies import run_enrich_companies

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            summary = await run_enrich_companies(
                session,
                max_companies=limit,
                refetch_after_days=refetch_after_days,
            )
            click.echo(summary.model_dump_json(indent=2))

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
    import logging

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
def ingest_news(lookback_days: int, no_techcrunch: bool, limit: int | None) -> None:
    """Pull funding-keyword news articles into the news_articles table."""
    import asyncio

    from nous.config import Settings
    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.ingest_news import run_ingest_news
    from nous.sources.news import NewsClient

    settings = Settings()

    async def _run() -> None:
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
            )
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("extract-funding")
@click.option(
    "--limit",
    type=int,
    default=1000,
    show_default=True,
    help="Maximum number of articles to process per run (weekly Gemini budget).",
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
    from nous.pipeline.extract_funding import run_extract_funding

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            summary = await run_extract_funding(
                session,
                limit=limit,
                skip_low_confidence=not include_low_confidence,
            )
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())


@cli.command("analyze-competitors")
def analyze_competitors() -> None:
    _stub("analyze-competitors")


@cli.command("estimate-employees")
def estimate_employees() -> None:
    _stub("estimate-employees")


@cli.command("diagnose-gemini")
@click.option(
    "--n",
    type=int,
    default=3,
    show_default=True,
    help="Number of tiny back-to-back test requests to fire.",
)
def diagnose_gemini(n: int) -> None:
    """Hit Gemini with N tiny requests and dump per-call timing + raw errors.

    Use this to see exactly which quota dimension trips (per-minute RPM,
    per-day RPD, or token-per-minute TPM) when the enrich-companies stage
    appears to rate-limit early. Reads GEMINI_API_KEY from your local .env.
    """
    import asyncio
    import time
    from datetime import UTC, datetime

    from pydantic import BaseModel

    from nous.config import Settings
    from nous.llm.client import (
        LLMError,
        LLMParseError,
        LLMRateLimitError,
        complete_json,
    )

    settings = Settings()
    if not settings.GEMINI_API_KEY:
        click.echo("ERROR: GEMINI_API_KEY is not set in your environment / .env")
        raise SystemExit(1)

    class TinyResponse(BaseModel):
        word: str

    prompt = (
        "Reply with a JSON object {\"word\": \"<a single random English noun>\"}. "
        "Return ONLY the JSON object, no surrounding text."
    )

    async def _run() -> None:
        click.echo(f"Started at {datetime.now(tz=UTC).isoformat()} UTC")
        click.echo(f"Firing {n} requests against gemini-2.5-flash with a ~3-token prompt.")
        click.echo("---")
        for i in range(1, n + 1):
            t0 = time.monotonic()
            try:
                result = await complete_json(prompt, TinyResponse)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                click.echo(
                    f"[{i}/{n}] OK in {elapsed_ms}ms — word={result.word!r}"
                )
            except LLMRateLimitError as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                click.echo(
                    f"[{i}/{n}] RATE LIMIT (HTTP 429) in {elapsed_ms}ms"
                )
                click.echo(f"        Raw error: {exc}")
                click.echo(
                    "        ^ Check the message above for the specific quota"
                    " dimension (per-minute RPM / per-day RPD / TPM)."
                )
                break
            except LLMParseError as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                click.echo(f"[{i}/{n}] PARSE ERROR in {elapsed_ms}ms: {exc}")
            except LLMError as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                click.echo(f"[{i}/{n}] LLM ERROR in {elapsed_ms}ms: {exc}")
        click.echo("---")
        click.echo(
            "Next step: open https://aistudio.google.com/rate-limit?timeRange=last-28-days"
            " to see your account's current quota usage."
        )

    asyncio.run(_run())


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
