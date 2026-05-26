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
def resolve_homepages() -> None:
    _stub("resolve-homepages")


@cli.command("scrape-homepages")
def scrape_homepages() -> None:
    _stub("scrape-homepages")


@cli.command("enrich-companies")
def enrich_companies() -> None:
    _stub("enrich-companies")


@cli.command("ingest-news")
def ingest_news() -> None:
    _stub("ingest-news")


@cli.command("extract-funding")
def extract_funding() -> None:
    _stub("extract-funding")


@cli.command("analyze-competitors")
def analyze_competitors() -> None:
    _stub("analyze-competitors")


@cli.command("estimate-employees")
def estimate_employees() -> None:
    _stub("estimate-employees")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
