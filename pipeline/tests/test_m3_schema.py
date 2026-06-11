"""Round-trip tests for M3 schema additions.

Covers:
- companies.discovered_via column (accepts explicit values)
- news_articles round-trip + url uniqueness
- funding_rounds round-trip
- investors round-trip + name_normalized uniqueness
- funding_round_investors join behavior + (round, investor) uniqueness
- pg_trgm GIN trigram similarity query

Requires DATABASE_URL pointing at a live Postgres with the schema applied
via `alembic upgrade head`. Tests are skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    FundingRound,
    FundingRoundInvestor,
    Investor,
    NewsArticle,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_company(**kwargs: object) -> Company:
    defaults: dict[str, object] = {
        "name": "M3 Test Co",
        "slug": f"m3-test-co-{os.urandom(3).hex()}",
        "normalized_name": "m3 test co",
        "hq_country": "US",
        "discovered_via": "vc_portfolio",
    }
    defaults.update(kwargs)
    return Company(**defaults)


# ---------------------------------------------------------------------------
# discovered_via
# ---------------------------------------------------------------------------


async def test_company_discovered_via_accepts_explicit_value(db: AsyncSession) -> None:
    """discovered_via accepts the M3 source values."""
    company = make_company(discovered_via="vc_portfolio")
    db.add(company)
    await db.flush()

    fetched = await db.get(Company, company.id)
    assert fetched is not None
    assert fetched.discovered_via == "vc_portfolio"


# ---------------------------------------------------------------------------
# news_articles
# ---------------------------------------------------------------------------


async def test_news_article_insert_and_read(db: AsyncSession) -> None:
    company = make_company()
    db.add(company)
    await db.flush()

    article = NewsArticle(
        company_id=company.id,
        url="https://techcrunch.com/2026/05/01/m3-test-co-raises/",
        title="M3 Test Co raises $50M Series A",
        source="techcrunch.com",
        published_date=date(2026, 5, 1),
        raw_content="M3 Test Co announced today...",
    )
    db.add(article)
    await db.flush()

    fetched = await db.get(NewsArticle, article.id)
    assert fetched is not None
    assert isinstance(fetched.id, UUID)
    assert fetched.url == "https://techcrunch.com/2026/05/01/m3-test-co-raises/"
    assert fetched.title == "M3 Test Co raises $50M Series A"
    assert fetched.source == "techcrunch.com"
    assert fetched.published_date == date(2026, 5, 1)
    assert fetched.processed is False  # server default
    assert fetched.created_at is not None


async def test_news_article_url_uniqueness(db: AsyncSession) -> None:
    """Two news_articles with the same URL violate the unique constraint."""
    company = make_company()
    db.add(company)
    await db.flush()

    url = "https://example.com/dupe-test"
    db.add(
        NewsArticle(
            company_id=company.id,
            url=url,
            title="First",
            source="example.com",
            raw_content="first",
        )
    )
    await db.flush()

    db.add(
        NewsArticle(
            company_id=company.id,
            url=url,
            title="Second",
            source="example.com",
            raw_content="second",
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


# ---------------------------------------------------------------------------
# funding_rounds
# ---------------------------------------------------------------------------


async def test_funding_round_insert_and_read(db: AsyncSession) -> None:
    company = make_company()
    db.add(company)
    await db.flush()

    fr = FundingRound(
        company_id=company.id,
        round_type="Series A",
        amount_raised=Decimal("50000000.00"),
        valuation_post_money=Decimal("300000000.00"),
        valuation_source="TechCrunch, May 2026",
        announced_date=date(2026, 5, 1),
        primary_news_url="https://techcrunch.com/2026/05/01/article",
        extraction_confidence="high",
    )
    db.add(fr)
    await db.flush()

    fetched = await db.get(FundingRound, fr.id)
    assert fetched is not None
    assert fetched.company_id == company.id
    assert fetched.round_type == "Series A"
    assert fetched.amount_raised == Decimal("50000000.00")
    assert fetched.valuation_post_money == Decimal("300000000.00")
    assert fetched.announced_date == date(2026, 5, 1)
    assert fetched.extraction_confidence == "high"


# ---------------------------------------------------------------------------
# investors + join table
# ---------------------------------------------------------------------------


async def test_investor_insert_and_read(db: AsyncSession) -> None:
    inv = Investor(
        name="Lightspeed Venture Partners",
        name_normalized="lightspeed venture partners",
        type="institutional",
        website="https://lsvp.com",
    )
    db.add(inv)
    await db.flush()

    fetched = await db.get(Investor, inv.id)
    assert fetched is not None
    assert fetched.name == "Lightspeed Venture Partners"
    assert fetched.name_normalized == "lightspeed venture partners"
    assert fetched.type == "institutional"


async def test_investor_name_normalized_uniqueness(db: AsyncSession) -> None:
    db.add(Investor(name="Sequoia", name_normalized="sequoia"))
    await db.flush()

    db.add(Investor(name="SEQUOIA Capital", name_normalized="sequoia"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_funding_round_investor_link_and_uniqueness(db: AsyncSession) -> None:
    company = make_company()
    db.add(company)
    await db.flush()

    fr = FundingRound(company_id=company.id, round_type="Seed")
    db.add(fr)
    inv = Investor(name="YC", name_normalized="yc")
    db.add(inv)
    await db.flush()

    link = FundingRoundInvestor(
        funding_round_id=fr.id,
        investor_id=inv.id,
        is_lead=True,
    )
    db.add(link)
    await db.flush()

    fetched = await db.get(FundingRoundInvestor, link.id)
    assert fetched is not None
    assert fetched.is_lead is True

    # Second link with same (round, investor) violates uniqueness
    db.add(
        FundingRoundInvestor(
            funding_round_id=fr.id,
            investor_id=inv.id,
            is_lead=False,
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


# ---------------------------------------------------------------------------
# pg_trgm trigram similarity
# ---------------------------------------------------------------------------


async def test_pg_trgm_similarity_query(db: AsyncSession) -> None:
    """The GIN trigram index makes similarity() queries on normalized_name
    return high-similarity matches above the M3 default threshold (0.85).
    """
    # Two near-identical normalized names. Exact-form pre-existing rows so
    # we know the trigram match has something to find.
    db.add(
        make_company(
            name="Recursive AI Inc.",
            slug=f"recursive-ai-{os.urandom(3).hex()}",
            normalized_name="recursive ai",
        )
    )
    await db.flush()
    await db.commit()

    # Same exact name → similarity = 1.0 (well above 0.85).
    result = await db.execute(
        select(
            Company.name,
            func.similarity(Company.normalized_name, "recursive ai").label("score"),
        ).where(func.similarity(Company.normalized_name, "recursive ai") >= 0.85)
    )
    rows = result.all()
    assert len(rows) >= 1
    assert rows[0].score >= 0.85


async def test_pg_trgm_extension_is_installed(db: AsyncSession) -> None:
    """Sanity check: the migration installed pg_trgm."""
    result = await db.execute(
        text("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
    )
    rows = result.all()
    assert len(rows) == 1
