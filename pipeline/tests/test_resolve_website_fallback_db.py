"""DB-gated integration tests for the resolve-website-fallback stage.

Requires DATABASE_URL at schema head (same gating as the other stage suites).
Source clients are faked (no network) so the tests exercise selection,
idempotency, the write path + provenance, rejected_urls handling, dry-run
safety, and the news-outbound path over real rows. The pure source cores are
unit-tested in test_wikidata_source.py / test_article_links.py.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle
from nous.pipeline import resolve_website_fallback as rwf
from nous.pipeline.resolve_website_fallback import run_resolve_website_fallback
from nous.sources.wikidata import WikidataMatch
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_UA = "nous-test (test@example.com)"


def _husk(name: str, **kwargs: object) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{normalize_name(name) or 'company'}-{suffix}",
        normalized_name=normalize_name(name),
        website=None,
        **kwargs,  # type: ignore[arg-type]
    )


def _fake_wikidata(mapping: dict[str, str | None]) -> type:
    """A WikidataClient stand-in returning canned websites keyed by name."""

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def __aenter__(self) -> _Fake:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def official_website(
            self, name: str, *, limit: int = 5
        ) -> WikidataMatch | None:
            website = mapping.get(name)
            if not website:
                return None
            return WikidataMatch(
                qid="Q1",
                entity_url="https://www.wikidata.org/wiki/Q1",
                website=website,
                matched_label=name,
            )

    return _Fake


def _fake_news(html_by_url: dict[str, str]) -> type:
    """A NewsClient stand-in returning canned article HTML keyed by url."""

    class _Fake:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def __aenter__(self) -> _Fake:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def fetch_text(self, url: str) -> str:
            return html_by_url.get(url, "<html></html>")

    return _Fake


async def test_selection_respects_eligibility(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only website-less, non-excluded, not-recently-checked rows are selected."""
    old = datetime.now(UTC) - timedelta(days=200)
    recent = datetime.now(UTC) - timedelta(days=1)

    eligible_a = _husk("Perplexity")
    eligible_b = _husk("Mistral", website_fallback_checked_at=old)
    has_site = _husk("Anthropic")
    has_site.website = "https://anthropic.com/"
    excluded = _husk("Foreign Co", exclusion_reason="non_us")
    recently_checked = _husk("Hebbia", website_fallback_checked_at=recent)
    for c in (eligible_a, eligible_b, has_site, excluded, recently_checked):
        db.add(c)
    await db.commit()

    monkeypatch.setattr(rwf, "WikidataClient", _fake_wikidata({}))
    summary = await run_resolve_website_fallback(
        db, user_agent=_UA, sources=("wikidata",), dry_run=True
    )

    seen_slugs = {r.slug for r in summary.resolutions}
    assert eligible_a.slug in seen_slugs
    assert eligible_b.slug in seen_slugs
    assert has_site.slug not in seen_slugs
    assert excluded.slug not in seen_slugs
    assert recently_checked.slug not in seen_slugs


async def test_apply_writes_website_and_provenance(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co = _husk("Perplexity")
    db.add(co)
    await db.commit()
    co_id = co.id

    monkeypatch.setattr(
        rwf,
        "WikidataClient",
        _fake_wikidata({"Perplexity": "https://www.perplexity.ai/"}),
    )
    summary = await run_resolve_website_fallback(
        db, user_agent=_UA, sources=("wikidata",)
    )

    assert summary.resolved == 1
    refreshed = (
        await db.execute(select(Company).where(Company.id == co_id))
    ).scalar_one()
    assert refreshed.website == "https://www.perplexity.ai/"
    assert refreshed.website_source == "wikidata"
    assert refreshed.website_source_url == "https://www.wikidata.org/wiki/Q1"
    assert refreshed.website_resolved_at is not None
    assert refreshed.website_fallback_checked_at is not None


async def test_miss_stamps_only_checked_at(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co = _husk("Nowhere Corp")
    db.add(co)
    await db.commit()
    co_id = co.id

    monkeypatch.setattr(rwf, "WikidataClient", _fake_wikidata({}))
    summary = await run_resolve_website_fallback(
        db, user_agent=_UA, sources=("wikidata",)
    )

    assert summary.missed == 1
    assert summary.resolved == 0
    refreshed = (
        await db.execute(select(Company).where(Company.id == co_id))
    ).scalar_one()
    assert refreshed.website is None
    assert refreshed.website_resolved_at is None  # untouched — resolve-homepages' stamp
    assert refreshed.website_fallback_checked_at is not None  # our back-off stamp


async def test_dry_run_writes_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co = _husk("Perplexity")
    db.add(co)
    await db.commit()
    co_id = co.id

    monkeypatch.setattr(
        rwf,
        "WikidataClient",
        _fake_wikidata({"Perplexity": "https://www.perplexity.ai/"}),
    )
    summary = await run_resolve_website_fallback(
        db, user_agent=_UA, sources=("wikidata",), dry_run=True
    )

    assert summary.resolved == 1  # would resolve
    refreshed = (
        await db.execute(select(Company).where(Company.id == co_id))
    ).scalar_one()
    assert refreshed.website is None
    assert refreshed.website_fallback_checked_at is None


async def test_rejected_url_not_accepted(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate whose domain is already in rejected_urls is never written."""
    co = _husk("Acme", rejected_urls=["https://acme.com/"])
    db.add(co)
    await db.commit()
    co_id = co.id

    monkeypatch.setattr(
        rwf, "WikidataClient", _fake_wikidata({"Acme": "https://acme.com/"})
    )
    summary = await run_resolve_website_fallback(
        db, user_agent=_UA, sources=("wikidata",)
    )

    assert summary.resolved == 0
    assert summary.missed == 1
    assert summary.per_source[0].candidate_found == 1
    assert summary.per_source[0].accepted == 0
    refreshed = (
        await db.execute(select(Company).where(Company.id == co_id))
    ).scalar_one()
    assert refreshed.website is None


async def test_idempotent_second_run_skips_resolved(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co = _husk("Perplexity")
    db.add(co)
    await db.commit()

    monkeypatch.setattr(
        rwf,
        "WikidataClient",
        _fake_wikidata({"Perplexity": "https://www.perplexity.ai/"}),
    )
    first = await run_resolve_website_fallback(db, user_agent=_UA, sources=("wikidata",))
    assert first.resolved == 1

    second = await run_resolve_website_fallback(
        db, user_agent=_UA, sources=("wikidata",)
    )
    assert second.companies_seen == 0  # website now set → out of the cohort


async def test_news_outbound_path(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co = _husk("Acme")
    db.add(co)
    await db.flush()
    article_url = "https://techcrunch.com/2026/07/01/acme-raises"
    db.add(
        NewsArticle(
            company_id=co.id,
            url=article_url,
            title="Acme raises $20M",
            source="techcrunch.com",
            published_date=datetime.now(UTC).date(),
            raw_content="Acme, the widget company, raised a round.",
        )
    )
    await db.commit()
    co_id = co.id

    html = '<p>Read more at <a href="https://acme.io">Acme</a>.</p>'
    monkeypatch.setattr(rwf, "NewsClient", _fake_news({article_url: html}))
    summary = await run_resolve_website_fallback(
        db, user_agent=_UA, sources=("news_outbound",)
    )

    assert summary.resolved == 1
    refreshed = (
        await db.execute(select(Company).where(Company.id == co_id))
    ).scalar_one()
    assert refreshed.website == "https://acme.io/"
    assert refreshed.website_source == "news_outbound"
    assert refreshed.website_source_url == article_url
