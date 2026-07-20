"""DB-gated integration tests for the refetch-article-text stage.

Requires DATABASE_URL at schema head (same gating as the other stage suites).
The shared resolve+fetch helper is monkeypatched (no network) so the tests
exercise selection, the heal-and-stamp write path, the stamp-only path on a
failed fetch, dry-run safety, and idempotency over real rows. The helper's own
resolve/fetch/failure shapes are unit-tested in test_news.py.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle
from nous.pipeline import refetch_article_text as rat
from nous.pipeline.refetch_article_text import run_refetch_article_text
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_UA = "nous-test (test@example.com)"
_HEALTHY = "x" * 600  # >= MIN_BODY_CHARS (500)
_THIN = "too short"  # < MIN_BODY_CHARS
_GN_URL = "https://news.google.com/rss/articles/CBMiOPAQUE?oc=5"
_DIRECT_THIN_URL = "https://pub.example.com/thin-article"
_DIRECT_HEALTHY_URL = "https://pub.example.com/full-article"


def _company(name: str, *, latest_round_amount: Decimal | None = None) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{normalize_name(name) or 'company'}-{suffix}",
        normalized_name=normalize_name(name),
        latest_round_amount=latest_round_amount,
    )


def _article(company_id: object, url: str, content: str, **kwargs: object) -> NewsArticle:
    return NewsArticle(
        company_id=company_id,
        url=url,
        title="Some headline",
        source="example.com",
        raw_content=content,
        **kwargs,  # type: ignore[arg-type]
    )


def _patch_helper(
    monkeypatch: pytest.MonkeyPatch, results: dict[str, tuple[str | None, str | None]]
) -> list[str]:
    """Patch the shared helper to return canned (url, text) by article URL.

    Returns a list that records every fetched URL so tests can assert dry-run
    made no calls.
    """
    fetched: list[str] = []

    async def _fake(client: object, url: str) -> tuple[str | None, str | None]:
        fetched.append(url)
        return results.get(url, (None, None))

    monkeypatch.setattr(rat, "resolve_and_fetch_article_text", _fake)
    return fetched


async def test_selection_picks_gn_and_thin_skips_healthy(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GN-host URL (any length) and a thin non-GN row are candidates; a healthy
    non-GN row is not."""
    company = _company("Sel Co")
    db.add(company)
    await db.flush()
    db.add_all(
        [
            _article(company.id, _GN_URL, _HEALTHY),  # GN host → picked
            _article(company.id, _DIRECT_THIN_URL, _THIN),  # thin → picked
            _article(company.id, _DIRECT_HEALTHY_URL, _HEALTHY),  # healthy → skipped
        ]
    )
    await db.commit()

    _patch_helper(monkeypatch, {})
    summary = await run_refetch_article_text(db, dry_run=True, limit=50)

    assert summary.selected == 2
    assert set(summary.sample_urls) == {_GN_URL, _DIRECT_THIN_URL}
    assert _DIRECT_HEALTHY_URL not in summary.sample_urls


async def test_stamped_row_not_repicked(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thin row already stamped (text_refetched_at set) is not selected again."""
    company = _company("Stamp Co")
    db.add(company)
    await db.flush()
    db.add(
        _article(
            company.id,
            _DIRECT_THIN_URL,
            _THIN,
            text_refetched_at=datetime.now(tz=UTC),
        )
    )
    await db.commit()

    _patch_helper(monkeypatch, {})
    summary = await run_refetch_article_text(db, dry_run=True, limit=50)

    assert summary.selected == 0


async def test_apply_heals_raw_content_and_stamps(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a healed fetch, raw_content is overwritten, text_refetched_at stamped,
    and the URL left untouched (dedup identity)."""
    company = _company("Heal Co")
    db.add(company)
    await db.flush()
    art = _article(company.id, _GN_URL, _HEALTHY)  # GN host, junk stored text
    db.add(art)
    await db.commit()

    healed = "REAL PUBLISHER PROSE " * 40  # >= 500
    fetched = _patch_helper(
        monkeypatch, {_GN_URL: ("https://realpub.com/story", healed)}
    )
    summary = await run_refetch_article_text(db, user_agent=_UA, dry_run=False, limit=50)

    assert summary.refetched == 1
    assert summary.failed_fetch == 0
    assert fetched == [_GN_URL]

    refreshed = (
        await db.execute(select(NewsArticle).where(NewsArticle.id == art.id))
    ).scalar_one()
    assert refreshed.raw_content == healed
    assert refreshed.text_refetched_at is not None
    assert refreshed.url == _GN_URL  # URL never changes


async def test_apply_failed_fetch_stamps_without_touching_raw_content(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thin/failed/robots fetch (helper → (None, None)) stamps the row but
    leaves raw_content unchanged."""
    company = _company("Fail Co")
    db.add(company)
    await db.flush()
    art = _article(company.id, _DIRECT_THIN_URL, _THIN)
    db.add(art)
    await db.commit()

    _patch_helper(monkeypatch, {_DIRECT_THIN_URL: (None, None)})
    summary = await run_refetch_article_text(db, user_agent=_UA, dry_run=False, limit=50)

    assert summary.refetched == 0
    assert summary.failed_fetch == 1

    refreshed = (
        await db.execute(select(NewsArticle).where(NewsArticle.id == art.id))
    ).scalar_one()
    assert refreshed.raw_content == _THIN  # unchanged
    assert refreshed.text_refetched_at is not None  # stamped anyway


async def test_dry_run_writes_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run makes no fetches and no writes."""
    company = _company("Dry Co")
    db.add(company)
    await db.flush()
    art = _article(company.id, _DIRECT_THIN_URL, _THIN)
    db.add(art)
    await db.commit()

    fetched = _patch_helper(monkeypatch, {_DIRECT_THIN_URL: (None, "would heal " * 60)})
    summary = await run_refetch_article_text(db, dry_run=True, limit=50)

    assert summary.selected == 1
    assert fetched == []  # no network in dry-run

    refreshed = (
        await db.execute(select(NewsArticle).where(NewsArticle.id == art.id))
    ).scalar_one()
    assert refreshed.raw_content == _THIN
    assert refreshed.text_refetched_at is None


async def test_idempotent_second_run_selects_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an apply run stamps every candidate, a second run at the same limit
    selects nothing."""
    company = _company("Idem Co")
    db.add(company)
    await db.flush()
    db.add_all(
        [
            _article(company.id, _GN_URL, _HEALTHY),
            _article(company.id, _DIRECT_THIN_URL, _THIN),
        ]
    )
    await db.commit()

    _patch_helper(
        monkeypatch,
        {
            _GN_URL: ("https://realpub.com/a", "healed " * 100),
            _DIRECT_THIN_URL: (None, None),
        },
    )
    first = await run_refetch_article_text(db, user_agent=_UA, dry_run=False, limit=50)
    assert first.selected == 2

    second = await run_refetch_article_text(db, dry_run=True, limit=50)
    assert second.selected == 0


async def test_apply_requires_user_agent(db: AsyncSession) -> None:
    """Apply mode without a contact-email user_agent raises (etiquette contract)."""
    company = _company("UA Co")
    db.add(company)
    await db.flush()
    db.add(_article(company.id, _DIRECT_THIN_URL, _THIN))
    await db.commit()

    with pytest.raises(ValueError, match="user_agent"):
        await run_refetch_article_text(db, user_agent="", dry_run=False, limit=50)
