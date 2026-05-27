"""Tests for the extract-funding stage.

DB-gated integration tests covering:
- Round + investor creation from a fixture FundingExtraction.
- Re-extraction within the proximity window merges into the existing round.
- Different round_type or out-of-window date creates a separate round.
- is_funding_announcement=False marks article processed without creating rounds.
- Low confidence skipped by default; opt-in via skip_low_confidence=False.
- limit caps articles per run.
- Lead-then-other for the same investor stays sticky-lead.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    FundingRound,
    FundingRoundInvestor,
    Investor,
    NewsArticle,
)
from nous.llm.prompts.funding_extraction import FundingExtraction
from nous.pipeline.extract_funding import run_extract_funding
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(name: str = "TestCo") -> Company:
    return Company(
        name=name,
        slug=f"{name.lower()}-{os.urandom(3).hex()}",
        normalized_name=normalize_name(name),
        hq_country="US",
    )


def _make_article(
    company_id: object,
    *,
    url: str,
    title: str = "Article",
    published: date | None = None,
) -> NewsArticle:
    return NewsArticle(
        company_id=company_id,  # type: ignore[arg-type]
        url=url,
        title=title,
        source="techcrunch.com",
        published_date=published,
        raw_content="Body of the article, used as prompt input. " * 30,
    )


def _make_extraction(
    *,
    is_funding: bool = True,
    round_type: str | None = "Series A",
    amount: Decimal | None = Decimal("50000000.00"),
    valuation: Decimal | None = Decimal("300000000.00"),
    valuation_source: str | None = None,
    announced: date | None = date(2026, 5, 1),
    leads: list[str] | None = None,
    others: list[str] | None = None,
    confidence: str = "high",
) -> FundingExtraction:
    return FundingExtraction(
        is_funding_announcement=is_funding,
        round_type=round_type,
        amount_raised_usd=amount,
        valuation_post_money_usd=valuation,
        valuation_source=valuation_source,
        announced_date=announced,
        lead_investors=leads if leads is not None else ["Lightspeed"],
        other_investors=others if others is not None else ["Founders Fund"],
        confidence=confidence,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------


async def test_extract_creates_round_and_investors(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company()
    db.add(company)
    await db.flush()
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/round-a",
            published=date(2026, 5, 1),
        )
    )
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction()

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.articles_processed == 1
    assert summary.funding_rounds_created == 1
    assert summary.investors_created == 2  # Lightspeed + Founders Fund
    assert summary.investor_links_created == 2

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].round_type == "Series A"
    assert rounds[0].amount_raised == Decimal("50000000.00")
    assert rounds[0].primary_news_url == "https://news.example.com/round-a"
    assert rounds[0].extraction_confidence == "high"


async def test_not_funding_announcement_marks_processed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company()
    db.add(company)
    await db.flush()
    article = _make_article(company.id, url="https://news.example.com/not-funding")
    db.add(article)
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(is_funding=False, leads=[], others=[])

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.skipped_not_funding == 1
    assert summary.funding_rounds_created == 0

    refetched = await db.get(NewsArticle, article.id)
    assert refetched is not None
    assert refetched.processed is True


async def test_low_confidence_skipped_by_default(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company()
    db.add(company)
    await db.flush()
    article = _make_article(company.id, url="https://news.example.com/low-conf")
    db.add(article)
    await db.flush()
    await db.commit()
    article_id = article.id

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(confidence="low")

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    summary = await run_extract_funding(db, limit=10, skip_low_confidence=True)
    assert summary.skipped_low_confidence == 1
    assert summary.funding_rounds_created == 0

    # The article must remain processed=False so a future run with a
    # tightened prompt (or --include-low-confidence) can retry. A low-
    # confidence extraction is a transient skip, not a terminal one.
    refetched = await db.get(NewsArticle, article_id)
    assert refetched is not None
    assert refetched.processed is False


async def test_low_confidence_included_with_opt_in(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company()
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/low-but-incl"))
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(confidence="low", leads=["Lightspeed"], others=[])

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    summary = await run_extract_funding(db, limit=10, skip_low_confidence=False)
    assert summary.funding_rounds_created == 1
    assert summary.investor_links_created == 1


async def test_valuation_source_is_persisted_when_extracted(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the LLM returns valuation_source, reconcile_funding_round writes it."""
    company = _make_company()
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/val-src"))
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            valuation=Decimal("750000000"),
            valuation_source="TechCrunch, March 2026",
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.funding_rounds_created == 1

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].valuation_source == "TechCrunch, March 2026"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


async def test_rerun_within_window_merges_round(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second article about the same round (within ±60 days, same round_type)
    should merge into the existing FundingRound, not create a new one.
    """
    company = _make_company()
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/round-1"))
    await db.flush()
    await db.commit()

    async def _fake_first(prompt: str, schema: type) -> FundingExtraction:
        # Round known, amount stated, no valuation.
        return _make_extraction(
            amount=Decimal("50000000.00"),
            valuation=None,
            confidence="medium",
            leads=["Lightspeed"],
            others=[],
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_first
    )

    s1 = await run_extract_funding(db, limit=10)
    assert s1.funding_rounds_created == 1

    # Second article — adds valuation + bumps confidence to high.
    db.add(_make_article(company.id, url="https://news.example.com/round-2"))
    await db.commit()

    async def _fake_second(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            amount=None,  # don't overwrite — null-fills only
            valuation=Decimal("300000000.00"),
            announced=date(2026, 5, 10),  # within 60 days of original
            confidence="high",
            leads=["Lightspeed"],
            others=["Sequoia"],
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_second
    )

    s2 = await run_extract_funding(db, limit=10)
    assert s2.funding_rounds_merged == 1
    assert s2.funding_rounds_created == 0

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    fr = rounds[0]
    # Amount stays from the first extraction
    assert fr.amount_raised == Decimal("50000000.00")
    # Valuation backfilled from the second
    assert fr.valuation_post_money == Decimal("300000000.00")
    # Confidence upgraded
    assert fr.extraction_confidence == "high"
    # primary_news_url stays first-write-wins
    assert fr.primary_news_url == "https://news.example.com/round-1"


async def test_different_round_type_creates_separate_round(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company()
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/series-a"))
    await db.flush()
    await db.commit()

    async def _fake_a(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(round_type="Series A")

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_a
    )
    await run_extract_funding(db, limit=10)

    db.add(_make_article(company.id, url="https://news.example.com/series-b"))
    await db.commit()

    async def _fake_b(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(round_type="Series B")

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_b
    )
    await run_extract_funding(db, limit=10)

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 2


# ---------------------------------------------------------------------------
# Investor link stickiness
# ---------------------------------------------------------------------------


async def test_lead_then_other_keeps_lead_true(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If one article names an investor as lead and a later article includes
    the same investor as a participant, the link stays is_lead=True.
    """
    company = _make_company()
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/first"))
    await db.flush()
    await db.commit()

    async def _fake_first(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(leads=["Sequoia"], others=[])

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_first
    )
    await run_extract_funding(db, limit=10)

    db.add(_make_article(company.id, url="https://news.example.com/second"))
    await db.commit()

    async def _fake_second(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            leads=[], others=["Sequoia"], announced=date(2026, 5, 5)
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_second
    )
    await run_extract_funding(db, limit=10)

    investors = (await db.execute(select(Investor))).scalars().all()
    # Only one Sequoia row despite being mentioned twice with different casing.
    assert sum(1 for i in investors if i.name_normalized == "sequoia") == 1

    links = (await db.execute(select(FundingRoundInvestor))).scalars().all()
    assert len(links) == 1
    assert links[0].is_lead is True


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------


async def test_limit_caps_articles_processed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company()
    db.add(company)
    await db.flush()
    for i in range(5):
        db.add(
            _make_article(
                company.id, url=f"https://news.example.com/limit-{i}"
            )
        )
    await db.flush()
    await db.commit()

    calls: list[Any] = []

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        calls.append(None)
        return _make_extraction(is_funding=False, leads=[], others=[])

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=2)
    assert summary.articles_processed == 2
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# CHECK constraint
# ---------------------------------------------------------------------------


async def test_extraction_confidence_check_rejects_invalid_values(
    db: AsyncSession,
) -> None:
    """The CHECK constraint blocks any string outside ('low','medium','high', NULL)."""
    from sqlalchemy.exc import IntegrityError

    company = _make_company()
    db.add(company)
    await db.flush()
    await db.commit()

    bad = FundingRound(
        company_id=company.id,
        round_type="Series A",
        amount_raised=Decimal("1000000"),
        announced_date=date(2026, 1, 1),
        extraction_confidence="medum",  # typo — must be rejected
    )
    db.add(bad)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()
