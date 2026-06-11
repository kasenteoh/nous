"""DB-gated integration tests for the cleanup-form-d migration stage.

Requires DATABASE_URL pointing at a Postgres with the schema at head.

Coverage:
- form_d + company_investors → re-tagged to 'vc_portfolio' (not deleted).
- form_d + news_articles (no investor) → re-tagged to 'news'.
- form_d + funding_rounds (no investor/news) → re-tagged to 'news'.
- form_d with no evidence → deleted (child rows cascade).
- already-'vc_portfolio' company → untouched.
- idempotency: second run returns 0/0/0 and changes nothing.
- dry_run: counts reported, rows unchanged.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    CompanyInvestor,
    FundingRound,
    NewsArticle,
)
from nous.db.upsert import upsert_investor
from nous.pipeline.cleanup_form_d import run_cleanup_form_d
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(name: str, *, discovered_via: str = "form_d") -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{normalize_name(name) or 'company'}-{suffix}",
        normalized_name=normalize_name(name),
        hq_country="US",
        discovered_via=discovered_via,
    )


def _add_news(db: AsyncSession, company_id: object) -> None:
    db.add(
        NewsArticle(
            company_id=company_id,
            url=f"https://news.example/{os.urandom(4).hex()}",
            title="Funding news",
            source="techcrunch.com",
            raw_content="body",
        )
    )


def _add_funding(db: AsyncSession, company_id: object) -> None:
    db.add(FundingRound(company_id=company_id, round_type="Seed"))


async def _discovered_via(db: AsyncSession, company_id: object) -> str | None:
    return (
        await db.execute(
            select(Company.discovered_via).where(Company.id == company_id)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Re-tag paths
# ---------------------------------------------------------------------------


async def test_form_d_with_investor_retagged_vc_portfolio(db: AsyncSession) -> None:
    """A form_d company that also has a company_investors row is re-tagged to
    'vc_portfolio' (VC evidence is the strongest signal) and not deleted."""
    company = _make_company("Investor-Backed Co")
    db.add(company)
    await db.flush()
    investor, _ = await upsert_investor(db, name=f"Seed Fund {os.urandom(3).hex()}")
    db.add(
        CompanyInvestor(
            company_id=company.id, investor_id=investor.id, source="vc_portfolio"
        )
    )
    await db.flush()
    await db.commit()
    company_id = company.id

    summary = await run_cleanup_form_d(db)

    assert summary.retagged_vc_portfolio == 1
    assert summary.retagged_news == 0
    assert summary.deleted == 0
    assert await _discovered_via(db, company_id) == "vc_portfolio"


async def test_form_d_with_news_retagged_news(db: AsyncSession) -> None:
    """A form_d company with a news article (no investor) is re-tagged to
    'news'."""
    company = _make_company("News-Covered Co")
    db.add(company)
    await db.flush()
    _add_news(db, company.id)
    await db.flush()
    await db.commit()
    company_id = company.id

    summary = await run_cleanup_form_d(db)

    assert summary.retagged_vc_portfolio == 0
    assert summary.retagged_news == 1
    assert summary.deleted == 0
    assert await _discovered_via(db, company_id) == "news"


async def test_form_d_with_funding_retagged_news(db: AsyncSession) -> None:
    """A form_d company with a funding round (no investor/news) is re-tagged to
    'news'."""
    company = _make_company("Funded Co")
    db.add(company)
    await db.flush()
    _add_funding(db, company.id)
    await db.flush()
    await db.commit()
    company_id = company.id

    summary = await run_cleanup_form_d(db)

    assert summary.retagged_vc_portfolio == 0
    assert summary.retagged_news == 1
    assert summary.deleted == 0
    assert await _discovered_via(db, company_id) == "news"


async def test_investor_evidence_wins_over_news(db: AsyncSession) -> None:
    """A form_d company with BOTH an investor link and news lands on the stronger
    'vc_portfolio' tag, not 'news'."""
    company = _make_company("Both-Signals Co")
    db.add(company)
    await db.flush()
    investor, _ = await upsert_investor(db, name=f"Both VC {os.urandom(3).hex()}")
    db.add(
        CompanyInvestor(
            company_id=company.id, investor_id=investor.id, source="vc_portfolio"
        )
    )
    _add_news(db, company.id)
    await db.flush()
    await db.commit()
    company_id = company.id

    summary = await run_cleanup_form_d(db)

    assert summary.retagged_vc_portfolio == 1
    assert summary.retagged_news == 0
    assert summary.deleted == 0
    assert await _discovered_via(db, company_id) == "vc_portfolio"


# ---------------------------------------------------------------------------
# Delete + untouched
# ---------------------------------------------------------------------------


async def test_form_d_without_evidence_deleted(db: AsyncSession) -> None:
    """A form_d company with no investor/news/funding evidence is deleted."""
    company = _make_company("Orphan Co")
    db.add(company)
    await db.flush()
    await db.commit()
    company_id = company.id

    summary = await run_cleanup_form_d(db)

    assert summary.retagged_vc_portfolio == 0
    assert summary.retagged_news == 0
    assert summary.deleted == 1
    assert await db.get(Company, company_id) is None


async def test_non_form_d_untouched(db: AsyncSession) -> None:
    """A company already tagged 'vc_portfolio' (even with no evidence) is left
    alone — the stage only touches form_d rows."""
    company = _make_company("Established Co", discovered_via="vc_portfolio")
    db.add(company)
    await db.flush()
    await db.commit()
    company_id = company.id

    summary = await run_cleanup_form_d(db)

    assert summary.retagged_vc_portfolio == 0
    assert summary.retagged_news == 0
    assert summary.deleted == 0
    assert await _discovered_via(db, company_id) == "vc_portfolio"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_second_run_is_noop(db: AsyncSession) -> None:
    """After a real run leaves no form_d rows, a second run is 0/0/0 and changes
    nothing."""
    keep_vc = _make_company("Keep VC Co")
    keep_news = _make_company("Keep News Co")
    drop = _make_company("Drop Co")
    db.add_all([keep_vc, keep_news, drop])
    await db.flush()
    investor, _ = await upsert_investor(db, name=f"Idem VC {os.urandom(3).hex()}")
    db.add(
        CompanyInvestor(
            company_id=keep_vc.id, investor_id=investor.id, source="vc_portfolio"
        )
    )
    _add_news(db, keep_news.id)
    await db.flush()
    await db.commit()
    keep_vc_id, keep_news_id, drop_id = keep_vc.id, keep_news.id, drop.id

    first = await run_cleanup_form_d(db)
    assert first.retagged_vc_portfolio == 1
    assert first.retagged_news == 1
    assert first.deleted == 1

    second = await run_cleanup_form_d(db)
    assert second.retagged_vc_portfolio == 0
    assert second.retagged_news == 0
    assert second.deleted == 0

    # State unchanged by the second run.
    assert await _discovered_via(db, keep_vc_id) == "vc_portfolio"
    assert await _discovered_via(db, keep_news_id) == "news"
    assert await db.get(Company, drop_id) is None


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


async def test_dry_run_reports_counts_without_writing(db: AsyncSession) -> None:
    """dry_run computes the same counts but performs no re-tag/delete."""
    vc = _make_company("DryRun VC Co")
    news = _make_company("DryRun News Co")
    drop = _make_company("DryRun Drop Co")
    db.add_all([vc, news, drop])
    await db.flush()
    investor, _ = await upsert_investor(db, name=f"Dry VC {os.urandom(3).hex()}")
    db.add(
        CompanyInvestor(
            company_id=vc.id, investor_id=investor.id, source="vc_portfolio"
        )
    )
    _add_news(db, news.id)
    await db.flush()
    await db.commit()
    vc_id, news_id, drop_id = vc.id, news.id, drop.id

    summary = await run_cleanup_form_d(db, dry_run=True)

    assert summary.retagged_vc_portfolio == 1
    assert summary.retagged_news == 1
    assert summary.deleted == 1

    # Nothing changed: all three rows are still form_d and present.
    assert await _discovered_via(db, vc_id) == "form_d"
    assert await _discovered_via(db, news_id) == "form_d"
    assert await _discovered_via(db, drop_id) == "form_d"
    remaining = (
        await db.execute(
            select(func.count())
            .select_from(Company)
            .where(Company.id.in_([vc_id, news_id, drop_id]))
        )
    ).scalar_one()
    assert remaining == 3
