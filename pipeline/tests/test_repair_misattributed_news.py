"""Tests for repair-misattributed-news — the retroactive aardvark-class purge.

The stage must delete exactly the stored articles that never mention their
company (per the live ingest guard) plus the rounds extracted FROM them —
and nothing else: the helix $10B article names the company in its body and
must survive alongside its round; dedup-alias names count as mentions;
dry-run writes nothing. Requires DATABASE_URL (skipped otherwise).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle, SlugAlias
from nous.pipeline.repair_misattributed_news import (
    run_repair_misattributed_news,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _co(name: str, slug: str, **kw: object) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        **kw,
    )


async def _seed_helix(db: AsyncSession) -> Company:
    """The prod shape: one correct article+round, one wrong-entity pair."""
    co = _co("Helix Digital Infrastructure Inc.", "helix-purge-test")
    db.add(co)
    await db.flush()

    good_url = "https://siliconangle.example.com/helix-launches-10b"
    bad_url = "https://siliconangle.example.com/kinoa-raises-10m"
    db.add_all(
        [
            NewsArticle(
                company_id=co.id,
                url=good_url,
                title="Helix launches with $10B+ in funding to build AI infrastructure",
                source="siliconangle.example.com",
                raw_content=(
                    "An investor consortium today launched a venture called "
                    "Helix Digital Infrastructure Inc. to build AI data "
                    "centers. Backers include KKR and Nvidia Corp."
                ),
            ),
            NewsArticle(
                company_id=co.id,
                url=bad_url,
                title="Kinoa pushes AI-native mobile app revenue operations after raising $10M",
                source="siliconangle.example.com",
                raw_content=(
                    "Kinoa, a mobile revenue operations startup, raised $10M "
                    "led by Transcend Fund."
                ),
            ),
        ]
    )
    db.add_all(
        [
            FundingRound(
                company_id=co.id,
                amount_raised=10_000_000_000,
                primary_news_url=good_url,
            ),
            FundingRound(
                company_id=co.id,
                amount_raised=10_000_000,
                primary_news_url=bad_url,
            ),
        ]
    )
    await db.commit()
    return co


async def test_purges_wrong_entity_article_and_round(db: AsyncSession) -> None:
    co = await _seed_helix(db)

    summary = await run_repair_misattributed_news(db, dry_run=False)
    assert summary.articles_deleted == 1
    assert summary.rounds_deleted == 1
    assert summary.companies_affected == 1

    articles = (
        (
            await db.execute(
                select(NewsArticle).where(NewsArticle.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    rounds = (
        (
            await db.execute(
                select(FundingRound).where(FundingRound.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    # The real $10B article + round survive (full name is in the body).
    assert len(articles) == 1 and "Helix launches" in articles[0].title
    assert len(rounds) == 1 and rounds[0].amount_raised == 10_000_000_000

    await db.refresh(co)
    assert co.funding_round_count == 1

    # Idempotent.
    second = await run_repair_misattributed_news(db, dry_run=False)
    assert second.articles_deleted == 0


async def test_dry_run_counts_but_writes_nothing(db: AsyncSession) -> None:
    co = await _seed_helix(db)
    summary = await run_repair_misattributed_news(db)  # dry_run default
    assert summary.dry_run is True
    assert summary.articles_deleted == 1
    assert summary.rounds_deleted == 1
    articles = (
        (
            await db.execute(
                select(NewsArticle).where(NewsArticle.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(articles) == 2


async def test_dictionary_word_garbage_purged(db: AsyncSession) -> None:
    """The away class: incidental-word articles fail the hardened guard and
    are purged; a genuine funding headline survives."""
    co = _co("Away", "away-purge-test")
    db.add(co)
    await db.flush()
    db.add_all(
        [
            NewsArticle(
                company_id=co.id,
                url="https://news.google.com/rss/articles/CBMiGARBAGE?oc=5",
                title="EU warns push to diversify away from China will need funding",
                source="news.google.com",
                raw_content="EU warns push to diversify away from China will need funding",
            ),
            NewsArticle(
                company_id=co.id,
                url="https://news.google.com/rss/articles/CBMiREAL?oc=5",
                title="Away raises $100M Series D to expand luggage line",
                source="news.google.com",
                raw_content="Away raises $100M Series D to expand luggage line",
            ),
        ]
    )
    await db.commit()

    summary = await run_repair_misattributed_news(db, dry_run=False)
    assert summary.articles_deleted == 1
    titles = (
        (
            await db.execute(
                select(NewsArticle.title).where(NewsArticle.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert titles == ["Away raises $100M Series D to expand luggage line"]


async def test_alias_name_counts_as_mention(db: AsyncSession) -> None:
    """A dedup survivor's older coverage referencing the merged-away name is
    NOT misattributed — slug_aliases names are accepted."""
    co = _co("Acme Inc", "acme-inc-purge")
    db.add(co)
    await db.flush()
    db.add(SlugAlias(old_slug="acme-labs", company_id=co.id))
    db.add(
        NewsArticle(
            company_id=co.id,
            url="https://techcrunch.example.com/acme-labs-seed",
            title="Acme Labs raises $5M seed",
            source="techcrunch.example.com",
            raw_content="Acme Labs, a devtools startup, raised $5M.",
        )
    )
    await db.commit()

    summary = await run_repair_misattributed_news(db, dry_run=False)
    assert summary.articles_deleted == 0


async def test_excluded_companies_untouched(db: AsyncSession) -> None:
    co = _co(
        "Gone Co",
        "gone-co-purge",
        exclusion_reason="manual",
    )
    db.add(co)
    await db.flush()
    db.add(
        NewsArticle(
            company_id=co.id,
            url="https://example.com/unrelated",
            title="Something entirely unrelated to funding",
            source="example.com",
            raw_content="nothing here",
        )
    )
    await db.commit()
    summary = await run_repair_misattributed_news(db, dry_run=False)
    assert summary.articles_deleted == 0
