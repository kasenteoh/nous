import click


@click.group()
def cli() -> None:
    """nous pipeline CLI."""


def _stub(stage: str) -> None:
    click.echo(f"{stage} not yet implemented")


@cli.command("ingest-filings")
def ingest_filings() -> None:
    _stub("ingest-filings")


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
