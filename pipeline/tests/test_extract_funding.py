"""Tests for the extract-funding stage.

DB-gated integration tests. For coverage by area see the section banners
throughout this file (Core extraction, Reconciliation, Investor link
stickiness, Limit, CHECK constraint, Website fallback, Status events,
Stated cumulative totals, --requery-totals one-time backfill, Website-path
bounded concurrency).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
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
    RawPage,
)
from nous.llm.client import LLMRateLimitError
from nous.llm.prompts.funding_extraction import FundingExtraction
from nous.pipeline.extract_funding import (
    run_extract_funding,
    run_extract_funding_website,
)
from nous.sources.news import ResolvedArticle
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
    raw_content: str | None = None,
    processed: bool = False,
) -> NewsArticle:
    return NewsArticle(
        company_id=company_id,  # type: ignore[arg-type]
        url=url,
        title=title,
        source="techcrunch.com",
        published_date=published,
        raw_content=raw_content
        or "Body of the article, used as prompt input. " * 30,
        processed=processed,
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
    status_event: str | None = None,
    status_confidence: str | None = None,
    total_raised: Decimal | None = None,
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
        status_event=status_event,  # type: ignore[arg-type]
        status_confidence=status_confidence,  # type: ignore[arg-type]
        total_raised_usd=total_raised,
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

    # The exact article→round link (0044): the processed article records which
    # round its extraction reconciled into (the web timeline groups by this).
    article = (
        await db.execute(
            select(NewsArticle).where(
                NewsArticle.url == "https://news.example.com/round-a"
            )
        )
    ).scalar_one()
    assert article.funding_round_id == rounds[0].id


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


async def test_extracts_post_money_valuation_and_total(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4 acceptance: an article like "Series B of $40M at a $400M post-money
    valuation, bringing total raised to $58M" lands the post-money valuation +
    its source on the FundingRound AND the stated cumulative total on the
    company (with a source URL). Mocked extraction — no live DeepSeek."""
    company = _make_company("ValTotalCo")
    db.add(company)
    await db.flush()
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/series-b-with-val-and-total",
            raw_content=(
                "ValTotalCo announced a Series B of $40M at a $400M post-money "
                "valuation, bringing total raised to $58M. " * 12
            ),
            published=date(2026, 6, 1),
        )
    )
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            round_type="Series B",
            amount=Decimal("40000000.00"),
            valuation=Decimal("400000000.00"),
            valuation_source="TechCrunch, June 2026",
            announced=date(2026, 6, 1),
            leads=["Acme Growth"],
            others=[],
            confidence="high",
            total_raised=Decimal("58000000.00"),
        )

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding(db, limit=10)
    assert summary.funding_rounds_created == 1
    assert summary.totals_recorded == 1

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].valuation_post_money == Decimal("400000000.00")
    assert rounds[0].valuation_source == "TechCrunch, June 2026"

    await db.refresh(company)
    assert company.total_raised_usd == Decimal("58000000.00")
    assert (
        company.total_raised_source_url
        == "https://news.example.com/series-b-with-val-and-total"
    )


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


async def test_backfill_creates_multiple_distinct_rounds(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-lookback backfill feeds several historical articles for ONE
    company — distinct round_type + announced_date years apart. extract-funding
    must create one FundingRound PER round (none merged), and a second run must
    create NO duplicates (Task A3 leans entirely on reconcile_funding_round's
    key; no new dedup logic). This is the depth lever: multi-row histories."""
    company = _make_company("TrajectoryCo")
    db.add(company)
    await db.flush()
    # Three historical funding articles, each a distinct round in a distinct
    # year — well outside the ±60-day proximity window, so they never merge.
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/seed-2022",
            raw_content="TrajectoryCo raised $3M Seed in 2022. " * 20,
            published=date(2022, 6, 1),
        )
    )
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/series-a-2023",
            raw_content="TrajectoryCo raised $15M Series A in 2023. " * 20,
            published=date(2023, 6, 1),
        )
    )
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/series-b-2024",
            raw_content="TrajectoryCo raised $40M Series B in 2024. " * 20,
            published=date(2024, 6, 1),
        )
    )
    await db.flush()
    await db.commit()

    # Route the extraction by the round named in the article body.
    def _extraction_for(text: str) -> FundingExtraction:
        if "Seed" in text:
            return _make_extraction(
                round_type="Seed",
                amount=Decimal("3000000.00"),
                valuation=None,
                announced=date(2022, 6, 1),
                leads=["Acme Seed Fund"],
                others=[],
            )
        if "Series A" in text:
            return _make_extraction(
                round_type="Series A",
                amount=Decimal("15000000.00"),
                valuation=None,
                announced=date(2023, 6, 1),
                leads=["Acme Ventures"],
                others=[],
            )
        return _make_extraction(
            round_type="Series B",
            amount=Decimal("40000000.00"),
            valuation=None,
            announced=date(2024, 6, 1),
            leads=["Big Growth"],
            others=[],
        )

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _extraction_for(prompt)

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    s1 = await run_extract_funding(db, limit=10)
    assert s1.funding_rounds_created == 3
    assert s1.funding_rounds_merged == 0

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 3
    assert {r.round_type for r in rounds} == {"Seed", "Series A", "Series B"}
    assert {r.announced_date for r in rounds} == {
        date(2022, 6, 1),
        date(2023, 6, 1),
        date(2024, 6, 1),
    }

    # Idempotency: re-feeding the SAME three articles (e.g. a re-dispatched
    # backfill, or fresh GN URLs for the same rounds) must merge into the
    # existing rows, never duplicate.
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/seed-2022-dup",
            raw_content="TrajectoryCo raised $3M Seed in 2022. " * 20,
            published=date(2022, 6, 15),  # within ±60 days of the original
        )
    )
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/series-a-2023-dup",
            raw_content="TrajectoryCo raised $15M Series A in 2023. " * 20,
            published=date(2023, 6, 15),
        )
    )
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/series-b-2024-dup",
            raw_content="TrajectoryCo raised $40M Series B in 2024. " * 20,
            published=date(2024, 6, 15),
        )
    )
    await db.commit()

    s2 = await run_extract_funding(db, limit=10)
    assert s2.funding_rounds_created == 0
    assert s2.funding_rounds_merged == 3

    rounds_after = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds_after) == 3  # still exactly three — no duplicates


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


# ---------------------------------------------------------------------------
# Website fallback (gap-fill only)
# ---------------------------------------------------------------------------


def _add_raw_page(company_id: object, url: str) -> RawPage:
    body = "The company raised a $20M Series B in March 2026. " * 20
    return RawPage(
        company_id=company_id,  # type: ignore[arg-type]
        url=url,
        content=f"<html><body><p>{body}</p></body></html>",
    )


async def test_website_fallback_creates_round_when_no_news(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company("WebCo")
    company.website = "https://webco.example/"
    db.add(company)
    await db.flush()
    db.add(_add_raw_page(company.id, "https://webco.example/about"))
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            round_type="Series B",
            amount=Decimal("20000000.00"),
            valuation=None,
            valuation_source="Company website, March 2026",
            leads=["Acme Capital"],
            others=[],
            confidence="medium",
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    summary = await run_extract_funding_website(db, limit=10)
    assert summary.companies_seen == 1
    assert summary.companies_with_funding == 1
    assert summary.funding_rounds_created == 1

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    # Attributed to the company's own website (the source of the text).
    assert rounds[0].primary_news_url == "https://webco.example/"
    assert rounds[0].valuation_source == "Company website, March 2026"


async def test_website_fallback_skips_company_with_existing_round(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap-fill only: a company that already has a funding_round is ineligible."""
    company = _make_company("HasRound")
    company.website = "https://hasround.example/"
    db.add(company)
    await db.flush()
    db.add(_add_raw_page(company.id, "https://hasround.example/about"))
    db.add(FundingRound(company_id=company.id, round_type="Seed"))
    await db.flush()
    await db.commit()

    calls = {"n": 0}

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        calls["n"] += 1
        return _make_extraction()

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    summary = await run_extract_funding_website(db, limit=10)
    assert summary.companies_seen == 0
    assert calls["n"] == 0


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


async def test_website_fallback_recently_checked_is_skipped(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company whose website-funding pass ran recently is excluded, so the
    daily gap-fill rotates through the backlog instead of re-LLM'ing the same
    alphabetical head (most sites never state funding, so without a marker a
    company with no round stays eligible forever)."""
    company = _make_company("RecentCheckCo")
    company.website_funding_checked_at = datetime.now(tz=UTC) - timedelta(days=1)
    db.add(company)
    await db.flush()
    db.add(_add_raw_page(company.id, "https://recentcheckco.com/"))
    await db.flush()
    await db.commit()

    calls: list[Any] = []

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        calls.append(None)
        return _make_extraction(is_funding=False, leads=[], others=[])

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding_website(db, limit=10)

    assert summary.companies_seen == 0
    assert calls == []


async def test_ignore_recheck_drains_recently_checked(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ignore_recheck=True`` drops the recheck-window predicate so a one-off
    drain mines EVERY round-less company's own site, even those checked moments
    ago (Task A2). The same company is skipped under the default
    ``ignore_recheck=False`` (still inside the 180-day back-off)."""
    company = _make_company("DrainMeCo")
    company.website = "https://drainmeco.example/"
    # Checked just now → inside the default 180-day back-off → normally skipped.
    company.website_funding_checked_at = datetime.now(tz=UTC)
    db.add(company)
    await db.flush()
    body = "DrainMeCo raised a $5M Seed round led by Acme Capital. " * 10
    db.add(
        RawPage(
            company_id=company.id,
            url="https://drainmeco.example/about",
            content=f"<html><body><p>{body}</p></body></html>",
        )
    )
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            round_type="Seed",
            amount=Decimal("5000000.00"),
            valuation=None,
            leads=["Acme Capital"],
            others=[],
            confidence="medium",
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    # Default: recently-checked company is skipped.
    default_summary = await run_extract_funding_website(db, limit=10)
    assert default_summary.companies_seen == 0
    assert default_summary.funding_rounds_created == 0

    # ignore_recheck=True: the same company is now processed and a round lands.
    drain_summary = await run_extract_funding_website(
        db, limit=10, ignore_recheck=True
    )
    assert drain_summary.companies_seen == 1
    assert drain_summary.companies_with_funding == 1
    assert drain_summary.funding_rounds_created == 1

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].round_type == "Seed"


async def test_website_fallback_stamps_attempt_even_without_funding(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """website_funding_checked_at is stamped on every attempt — including
    no-funding-found — so the rotation advances."""
    company = _make_company("StampCo")
    db.add(company)
    await db.flush()
    db.add(_add_raw_page(company.id, "https://stampco.com/"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(is_funding=False, leads=[], others=[])

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    await run_extract_funding_website(db, limit=10)

    await db.refresh(company)
    assert company.website_funding_checked_at is not None


# ---------------------------------------------------------------------------
# Status events (acquired / shut_down / ipo)
# ---------------------------------------------------------------------------


async def test_acquisition_article_sets_status_and_marks_processed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acquisition article is usually NOT a funding announcement; the status
    must still be applied (with the article as source) and the article must
    still be marked processed — same as any other non-funding article."""
    company = _make_company("AcquiredCo")
    db.add(company)
    await db.flush()
    article = _make_article(company.id, url="https://news.example.com/acquired")
    db.add(article)
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            status_event="acquired",
            status_confidence="high",
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.skipped_not_funding == 1
    assert summary.funding_rounds_created == 0
    assert summary.status_changes_applied == 1

    await db.refresh(company)
    assert company.status == "acquired"
    assert company.status_source_url == "https://news.example.com/acquired"

    refetched = await db.get(NewsArticle, article.id)
    assert refetched is not None
    assert refetched.processed is True


async def test_low_confidence_round_branch_commits_status_event(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A funding article with a LOW round confidence but a HIGH-confidence
    status event takes the transient-skip `continue` — which never reaches the
    end-of-loop commit, so the branch must commit the status itself.

    The conftest harness turns every `session.commit()` into a SAVEPOINT
    release inside an outer transaction, so rolling the session back after the
    run discards exactly the work the stage left uncommitted. If the in-branch
    commit were removed, the status change would die in that rollback and the
    re-fetch below would see 'active'."""
    company = _make_company("LowConfExitCo")
    db.add(company)
    await db.flush()
    article = _make_article(company.id, url="https://news.example.com/low-conf-exit")
    db.add(article)
    await db.flush()
    await db.commit()
    company_id = company.id
    article_id = article.id

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=True,
            confidence="low",
            status_event="acquired",
            status_confidence="high",
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10, skip_low_confidence=True)
    assert summary.status_changes_applied == 1
    assert summary.skipped_low_confidence == 1
    assert summary.funding_rounds_created == 0

    # Throw away anything the stage left pending: only explicitly committed
    # work survives this rollback.
    await db.rollback()

    refetched = await db.get(Company, company_id)
    assert refetched is not None
    assert refetched.status == "acquired"
    assert refetched.status_source_url == "https://news.example.com/low-conf-exit"

    # The ROUND extraction stays retryable: low confidence is a transient
    # skip, so the article must remain unprocessed.
    refetched_article = await db.get(NewsArticle, article_id)
    assert refetched_article is not None
    assert refetched_article.processed is False


async def test_low_confidence_status_event_is_ignored(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status_confidence='low' is noise — the company stays active."""
    company = _make_company("MaybeDeadCo")
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/maybe-dead"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            status_event="shut_down",
            status_confidence="low",
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.status_changes_applied == 0
    assert summary.status_sources_backfilled == 0

    await db.refresh(company)
    assert company.status == "active"
    assert company.status_source_url is None


async def test_status_event_never_downgrades_existing_status(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-active status is never overwritten — manual correction is the
    escape hatch. A later 'shut_down' article must not clobber 'acquired'."""
    company = _make_company("AlreadyAcquiredCo")
    company.status = "acquired"
    company.status_source_url = "https://news.example.com/original-acquisition"
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/shutdown-later"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            status_event="shut_down",
            status_confidence="high",
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.status_changes_applied == 0
    assert summary.status_sources_backfilled == 0

    await db.refresh(company)
    assert company.status == "acquired"
    assert (
        company.status_source_url == "https://news.example.com/original-acquisition"
    )

    # The article is still consumed by the queue.
    articles = (await db.execute(select(NewsArticle))).scalars().all()
    assert all(a.processed for a in articles)


async def test_same_status_reconfirmation_fills_null_source_url(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-confirming the SAME status may backfill a missing source URL (e.g.
    a manually set status without attribution) — but never replaces one. The
    status value itself does not change, so the backfill counts under
    status_sources_backfilled, NOT status_changes_applied."""
    company = _make_company("ManualAcquiredCo")
    company.status = "acquired"
    company.status_source_url = None
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/confirm-acq"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            status_event="acquired",
            status_confidence="medium",
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.status_changes_applied == 0
    assert summary.status_sources_backfilled == 1

    await db.refresh(company)
    assert company.status == "acquired"
    assert company.status_source_url == "https://news.example.com/confirm-acq"


async def test_null_status_event_leaves_company_active(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain funding extraction (status_event=None) never touches status."""
    company = _make_company("StillAliveCo")
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/normal-round"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction()

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.funding_rounds_created == 1
    assert summary.status_changes_applied == 0
    assert summary.status_sources_backfilled == 0

    await db.refresh(company)
    assert company.status == "active"
    assert company.status_source_url is None


async def test_website_status_event_applies_with_medium_confidence(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The website path applies an own-site status notice (prompt caps it at
    'medium', which passes the medium/high gate) with the company website as
    the source URL — and still stamps the rotation marker."""
    company = _make_company("WindDownCo")
    company.website = "https://winddownco.example/"
    db.add(company)
    await db.flush()
    db.add(_add_raw_page(company.id, "https://winddownco.example/about"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            status_event="shut_down",
            status_confidence="medium",
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding_website(db, limit=10)
    assert summary.companies_seen == 1
    assert summary.status_changes_applied == 1
    assert summary.funding_rounds_created == 0

    await db.refresh(company)
    assert company.status == "shut_down"
    assert company.status_source_url == "https://winddownco.example/"
    assert company.website_funding_checked_at is not None


async def test_companies_status_check_rejects_invalid_values(
    db: AsyncSession,
) -> None:
    """The CHECK constraint blocks any status outside the four known values."""
    from sqlalchemy.exc import IntegrityError

    company = _make_company("ZombieCo")
    company.status = "zombie"
    db.add(company)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


# ---------------------------------------------------------------------------
# Stated cumulative totals ("has raised $X to date")
# ---------------------------------------------------------------------------


async def test_total_raised_recorded_from_funding_article(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A funding article that also states a cumulative total records all three
    columns together: the figure, the article as source, and the article's
    published date as the as-of."""
    company = _make_company("FreshaLikeCo")
    db.add(company)
    await db.flush()
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/round-with-total",
            published=date(2026, 5, 20),
        )
    )
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            amount=Decimal("80000000.00"),
            total_raised=Decimal("285000000.00"),
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.totals_recorded == 1
    assert summary.funding_rounds_created == 1

    await db.refresh(company)
    assert company.total_raised_usd == Decimal("285000000.00")
    assert (
        company.total_raised_source_url
        == "https://news.example.com/round-with-total"
    )
    assert company.total_raised_as_of == date(2026, 5, 20)


async def test_total_raised_recorded_from_non_funding_article(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Totals appear in non-funding coverage too (e.g. acquisition articles
    recapping funding history) — the apply must run BEFORE the
    is_funding_announcement gate, and the article still gets consumed."""
    company = _make_company("AcqRecapCo")
    db.add(company)
    await db.flush()
    article = _make_article(
        company.id,
        url="https://news.example.com/acq-with-total",
        published=date(2026, 6, 1),
    )
    db.add(article)
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            total_raised=Decimal("285000000.00"),
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.totals_recorded == 1
    assert summary.skipped_not_funding == 1
    assert summary.funding_rounds_created == 0

    await db.refresh(company)
    assert company.total_raised_usd == Decimal("285000000.00")
    assert company.total_raised_source_url == "https://news.example.com/acq-with-total"

    refetched = await db.get(NewsArticle, article.id)
    assert refetched is not None
    assert refetched.processed is True


async def test_newer_article_total_supersedes_even_when_smaller(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Newest-article-wins: a newer article's stated total replaces an older
    claim even when the new figure is SMALLER — it is the most recent source
    claim, and the web tile shows max(stated, sum-of-rounds) anyway."""
    company = _make_company("SupersededCo")
    company.total_raised_usd = Decimal("300000000.00")
    company.total_raised_source_url = "https://news.example.com/old-claim"
    company.total_raised_as_of = date(2026, 4, 1)
    db.add(company)
    await db.flush()
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/new-claim",
            published=date(2026, 5, 20),
        )
    )
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            total_raised=Decimal("285000000.00"),
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.totals_recorded == 1

    await db.refresh(company)
    assert company.total_raised_usd == Decimal("285000000.00")
    assert company.total_raised_source_url == "https://news.example.com/new-claim"
    assert company.total_raised_as_of == date(2026, 5, 20)


async def test_older_or_same_day_article_total_is_ignored(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claims dated on or before the recorded as-of never overwrite — older
    coverage is stale, and same-day no-ops keep re-runs idempotent."""
    company = _make_company("FreshClaimCo")
    company.total_raised_usd = Decimal("285000000.00")
    company.total_raised_source_url = "https://news.example.com/current-claim"
    company.total_raised_as_of = date(2026, 5, 1)
    db.add(company)
    await db.flush()
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/same-day",
            published=date(2026, 5, 1),
        )
    )
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/older",
            published=date(2026, 4, 1),
        )
    )
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            total_raised=Decimal("100000000.00"),
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.articles_processed == 2
    assert summary.totals_recorded == 0

    await db.refresh(company)
    assert company.total_raised_usd == Decimal("285000000.00")
    assert company.total_raised_source_url == "https://news.example.com/current-claim"
    assert company.total_raised_as_of == date(2026, 5, 1)


async def test_dated_total_supersedes_existing_claim_with_null_as_of(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing claim with a NULL as_of (e.g. a manual edit) can't assert
    recency, so any dated claim supersedes it — the guard must never treat a
    missing as-of as blocking, and all three columns travel together."""
    company = _make_company("NullAsOfCo")
    company.total_raised_usd = Decimal("300000000.00")
    company.total_raised_source_url = "https://example.com/manual-edit"
    company.total_raised_as_of = None
    db.add(company)
    await db.flush()
    db.add(
        _make_article(
            company.id,
            url="https://news.example.com/dated-claim",
            published=date(2026, 5, 20),
        )
    )
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            total_raised=Decimal("285000000.00"),
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.totals_recorded == 1

    await db.refresh(company)
    assert company.total_raised_usd == Decimal("285000000.00")
    assert company.total_raised_source_url == "https://news.example.com/dated-claim"
    assert company.total_raised_as_of == date(2026, 5, 20)


async def test_total_never_fabricated_when_extraction_field_null(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain round extraction (total_raised_usd=None) never invents a total
    — the columns stay null, and the round amount is NOT copied over."""
    company = _make_company("NoTotalCo")
    db.add(company)
    await db.flush()
    db.add(_make_article(company.id, url="https://news.example.com/no-total"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction()

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.funding_rounds_created == 1
    assert summary.totals_recorded == 0

    await db.refresh(company)
    assert company.total_raised_usd is None
    assert company.total_raised_source_url is None
    assert company.total_raised_as_of is None


async def test_total_as_of_falls_back_to_today_when_published_null(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Articles without a published date still record the claim, dated today,
    so it participates in newest-wins ordering."""
    company = _make_company("UndatedArticleCo")
    db.add(company)
    await db.flush()
    db.add(
        _make_article(
            company.id, url="https://news.example.com/undated", published=None
        )
    )
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(total_raised=Decimal("50000000.00"))

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10)
    assert summary.totals_recorded == 1

    await db.refresh(company)
    assert company.total_raised_usd == Decimal("50000000.00")
    assert company.total_raised_as_of == datetime.now(tz=UTC).date()


async def test_low_confidence_round_branch_commits_total(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A low-confidence ROUND with a stated total takes the transient-skip
    `continue` — which never reaches the end-of-loop commit, so the branch
    must commit the total itself (same harness trick as the status-event
    commit test: rollback discards anything left uncommitted)."""
    company = _make_company("LowConfTotalCo")
    db.add(company)
    await db.flush()
    article = _make_article(
        company.id,
        url="https://news.example.com/low-conf-total",
        published=date(2026, 5, 20),
    )
    db.add(article)
    await db.flush()
    await db.commit()
    company_id = company.id
    article_id = article.id

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            confidence="low",
            total_raised=Decimal("285000000.00"),
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10, skip_low_confidence=True)
    assert summary.totals_recorded == 1
    assert summary.skipped_low_confidence == 1
    assert summary.funding_rounds_created == 0

    # Only explicitly committed work survives this rollback.
    await db.rollback()

    refetched = await db.get(Company, company_id)
    assert refetched is not None
    assert refetched.total_raised_usd == Decimal("285000000.00")
    assert refetched.total_raised_source_url == "https://news.example.com/low-conf-total"

    # The round extraction stays retryable.
    refetched_article = await db.get(NewsArticle, article_id)
    assert refetched_article is not None
    assert refetched_article.processed is False


async def test_website_path_records_total_with_today_as_of(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The website path records an own-site stated total ("we've raised $X")
    with the company website as source and today as the as-of — even when the
    page states no individual round."""
    company = _make_company("SiteTotalCo")
    company.website = "https://sitetotalco.example/"
    db.add(company)
    await db.flush()
    db.add(_add_raw_page(company.id, "https://sitetotalco.example/about"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            total_raised=Decimal("50000000.00"),
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding_website(db, limit=10)
    assert summary.companies_seen == 1
    assert summary.totals_recorded == 1
    assert summary.funding_rounds_created == 0

    await db.refresh(company)
    assert company.total_raised_usd == Decimal("50000000.00")
    assert company.total_raised_source_url == "https://sitetotalco.example/"
    assert company.total_raised_as_of == datetime.now(tz=UTC).date()
    assert company.website_funding_checked_at is not None


# ---------------------------------------------------------------------------
# --requery-totals one-time backfill
# ---------------------------------------------------------------------------


_TOTAL_PHRASE_BODY = (
    "Coverage of the acquisition. The company has now raised "
    "$285 million To Date, according to the announcement. " * 10
)


async def test_requery_totals_selects_only_matching_processed_null_total(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """requery_totals flips the selection to: PROCESSED articles whose text
    matches a cumulative-total phrase (case-insensitive) AND whose company has
    no stated total yet. Everything else — phrase-less processed articles,
    companies with a total already, unprocessed articles — is excluded. The
    re-run records the total and the article STAYS processed."""
    # Eligible: processed + phrase ("To Date", mixed case → ILIKE) + null total.
    eligible = _make_company("EligibleCo")
    db.add(eligible)
    await db.flush()
    eligible_article = _make_article(
        eligible.id,
        url="https://news.example.com/eligible",
        published=date(2026, 5, 20),
        raw_content=_TOTAL_PHRASE_BODY,
        processed=True,
    )
    db.add(eligible_article)

    # Excluded: processed but no total phrase in the body.
    no_phrase = _make_company("NoPhraseCo")
    db.add(no_phrase)
    await db.flush()
    db.add(
        _make_article(
            no_phrase.id,
            url="https://news.example.com/no-phrase",
            processed=True,
        )
    )

    # Excluded: company already has a stated total.
    has_total = _make_company("HasTotalCo")
    has_total.total_raised_usd = Decimal("10000000.00")
    has_total.total_raised_source_url = "https://news.example.com/prior"
    has_total.total_raised_as_of = date(2026, 1, 1)
    db.add(has_total)
    await db.flush()
    db.add(
        _make_article(
            has_total.id,
            url="https://news.example.com/has-total",
            raw_content=_TOTAL_PHRASE_BODY,
            processed=True,
        )
    )

    # Excluded: phrase matches but the article is still unprocessed (the
    # normal daily run owns it).
    unprocessed = _make_company("UnprocessedCo")
    db.add(unprocessed)
    await db.flush()
    db.add(
        _make_article(
            unprocessed.id,
            url="https://news.example.com/unprocessed",
            raw_content=_TOTAL_PHRASE_BODY,
            processed=False,
        )
    )
    await db.flush()
    await db.commit()

    prompts_seen: list[str] = []

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        prompts_seen.append(prompt)
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            total_raised=Decimal("285000000.00"),
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=10, requery_totals=True)
    assert len(prompts_seen) == 1  # only EligibleCo's article hit the LLM
    assert summary.articles_processed == 1
    assert summary.totals_recorded == 1

    await db.refresh(eligible)
    assert eligible.total_raised_usd == Decimal("285000000.00")
    assert eligible.total_raised_source_url == "https://news.example.com/eligible"
    assert eligible.total_raised_as_of == date(2026, 5, 20)

    # Untouched companies stay untouched.
    await db.refresh(has_total)
    assert has_total.total_raised_usd == Decimal("10000000.00")
    await db.refresh(no_phrase)
    assert no_phrase.total_raised_usd is None
    await db.refresh(unprocessed)
    assert unprocessed.total_raised_usd is None

    # The re-queried article stays processed.
    refetched = await db.get(NewsArticle, eligible_article.id)
    assert refetched is not None
    assert refetched.processed is True


async def test_requery_totals_respects_limit(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backfill is capped by --limit like the normal path (LLM budget)."""
    for i in range(3):
        company = _make_company(f"BackfillCo{i}")
        db.add(company)
        await db.flush()
        db.add(
            _make_article(
                company.id,
                url=f"https://news.example.com/backfill-{i}",
                raw_content=_TOTAL_PHRASE_BODY,
                processed=True,
            )
        )
    await db.flush()
    await db.commit()

    calls: list[Any] = []

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        calls.append(None)
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            total_raised=Decimal("285000000.00"),
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding(db, limit=2, requery_totals=True)
    assert len(calls) == 2
    assert summary.totals_recorded == 2


# ---------------------------------------------------------------------------
# Task 2.7.1 — funding-source quality: reject junk/image hosts
# ---------------------------------------------------------------------------


async def test_website_fallback_skips_imgur_source(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company whose website URL resolves to an image host (e.g. imgur.com)
    must NOT have a funding round persisted — the source is junk."""
    company = _make_company("ImgurCo")
    # Set the website to an image-host URL — the extraction would otherwise
    # attribute the round to this junk source.
    company.website = "https://imgur.com/gallery/some-funding-chart"
    db.add(company)
    await db.flush()
    body_text = "We raised $20M Series A led by Acme Capital. " * 10
    db.add(
        RawPage(
            company_id=company.id,
            url="https://imgur.com/gallery/some-funding-chart",
            content=f"<html><body><p>{body_text}</p></body></html>",
        )
    )
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            round_type="Series A",
            amount=Decimal("20000000.00"),
            confidence="medium",
            leads=["Acme Capital"],
            others=[],
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding_website(db, limit=10)
    assert summary.companies_seen == 1
    assert summary.skipped_junk_source == 1
    assert summary.companies_with_funding == 0
    assert summary.funding_rounds_created == 0

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 0


async def test_website_fallback_skips_aggregator_source(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company whose website URL is a known aggregator (crunchbase.com) must
    not have a round persisted — reuses is_aggregator_url."""
    company = _make_company("CrunchCo")
    company.website = "https://www.crunchbase.com/organization/crunchco"
    db.add(company)
    await db.flush()
    body_text = "CrunchCo raised $50M Series B led by Big VC. " * 10
    db.add(
        RawPage(
            company_id=company.id,
            url="https://www.crunchbase.com/organization/crunchco",
            content=f"<html><body><p>{body_text}</p></body></html>",
        )
    )
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            amount=Decimal("50000000.00"),
            confidence="medium",
            leads=[],
            others=[],
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding_website(db, limit=10)
    assert summary.skipped_junk_source == 1
    assert summary.funding_rounds_created == 0


async def test_website_fallback_accepts_own_domain(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company's own domain is NOT rejected — only third-party junk hosts are."""
    company = _make_company("LegitCo")
    company.website = "https://legitco.example/"
    db.add(company)
    await db.flush()
    db.add(_add_raw_page(company.id, "https://legitco.example/about"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            round_type="Series A",
            amount=Decimal("15000000.00"),
            confidence="medium",
            leads=["Good Capital"],
            others=[],
        )

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake
    )

    summary = await run_extract_funding_website(db, limit=10)
    assert summary.skipped_junk_source == 0
    assert summary.companies_with_funding == 1
    assert summary.funding_rounds_created == 1


# ---------------------------------------------------------------------------
# Website-path bounded concurrency (parity with the sequential behavior)
# ---------------------------------------------------------------------------


def _make_funding_company(name: str) -> Company:
    """A round-less company with a website + a funding-stating page — eligible
    for the website path and guaranteed to produce a round on a high extraction."""
    company = _make_company(name)
    company.website = f"https://{name.lower()}.example/"
    return company


async def test_website_concurrency_writes_same_rounds_as_sequential(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With concurrency > 1, each eligible company gets its round persisted and
    its checked-at stamped exactly as a sequential run would — one round per
    company, attributed to that company's own website."""
    n = 10
    companies = [_make_funding_company(f"WConcCo{i:02d}") for i in range(n)]
    db.add_all(companies)
    await db.flush()
    for c in companies:
        db.add(_add_raw_page(c.id, f"{c.website}about"))
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction(
            round_type="Series B",
            amount=Decimal("20000000.00"),
            valuation=None,
            leads=["Acme Capital"],
            others=[],
            confidence="medium",
        )

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding_website(db, limit=100, concurrency=5)
    assert summary.companies_seen == n
    assert summary.companies_with_funding == n
    assert summary.funding_rounds_created == n

    for c in companies:
        rounds = (
            (
                await db.execute(
                    select(FundingRound).where(FundingRound.company_id == c.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rounds) == 1
        assert rounds[0].primary_news_url == c.website
        await db.refresh(c)
        assert c.website_funding_checked_at is not None


async def test_website_concurrency_runs_llm_calls_in_parallel(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The website-funding LLM calls for a batch overlap in time (bounded by the
    semaphore). Proven by recording the peak concurrently-in-flight calls."""
    n = 8
    companies = [_make_funding_company(f"WParCo{i:02d}") for i in range(n)]
    db.add_all(companies)
    await db.flush()
    for c in companies:
        db.add(_add_raw_page(c.id, f"{c.website}about"))
    await db.flush()
    await db.commit()

    state = {"in_flight": 0, "peak": 0}

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            await asyncio.sleep(0.02)
            return _make_extraction(is_funding=False, leads=[], others=[])
        finally:
            state["in_flight"] -= 1

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    await run_extract_funding_website(db, limit=100, concurrency=4)
    assert state["peak"] >= 2
    assert state["peak"] <= 4  # never exceeds the semaphore bound


async def test_website_concurrency_one_is_strictly_sequential(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """concurrency=1 degrades to one-at-a-time: peak in-flight is never > 1."""
    n = 5
    companies = [_make_funding_company(f"WSeqCo{i}") for i in range(n)]
    db.add_all(companies)
    await db.flush()
    for c in companies:
        db.add(_add_raw_page(c.id, f"{c.website}about"))
    await db.flush()
    await db.commit()

    state = {"in_flight": 0, "peak": 0}

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            await asyncio.sleep(0.005)
            return _make_extraction(is_funding=False, leads=[], others=[])
        finally:
            state["in_flight"] -= 1

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding_website(db, limit=100, concurrency=1)
    assert summary.companies_seen == n
    assert state["peak"] == 1


async def test_website_concurrency_rate_limit_stops_and_does_not_stamp(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 stops scheduling further batches; the rate-limited company is NOT
    stamped (so it stays eligible next run) and later batches never hit the LLM,
    while first-batch companies are stamped + persisted.

    Companies are named so alphabetical selection order is deterministic; with
    concurrency 3 the batches are [00,01,02] [03,04,05] [06,07,08] [09].
    """
    n = 10
    companies = [_make_funding_company(f"WRlCo{i:02d}") for i in range(n)]
    db.add_all(companies)
    await db.flush()
    for c in companies:
        db.add(_add_raw_page(c.id, f"{c.website}about"))
    await db.flush()
    await db.commit()
    first_batch_ids = [companies[i].id for i in range(3)]
    rate_limited_id = companies[4].id

    seen: list[str] = []

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        for i in range(n):
            if f"WRlCo{i:02d}" in prompt:
                seen.append(f"WRlCo{i:02d}")
                if i == 4:
                    raise LLMRateLimitError("429")
                return _make_extraction(is_funding=False, leads=[], others=[])
        raise AssertionError("unknown company in prompt")

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding_website(db, limit=100, concurrency=3)

    assert summary.skipped_rate_limited == 1
    # Third+ batches never reached the LLM.
    for i in range(6, n):
        assert f"WRlCo{i:02d}" not in seen

    # First-batch companies are stamped (attempt completed).
    for cid in first_batch_ids:
        c = await db.get(Company, cid)
        assert c is not None
        assert c.website_funding_checked_at is not None

    # The rate-limited company is deliberately NOT stamped — it keeps its slot.
    rl = await db.get(Company, rate_limited_id)
    assert rl is not None
    assert rl.website_funding_checked_at is None


async def test_website_concurrency_thin_text_skips_without_llm(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company whose page text is below the minimum is counted under
    skipped_no_text, never sent to the LLM, and still stamped — preserved under
    concurrency alongside companies that do call the LLM."""
    # One thin-text company + two with substantial funding text.
    thin = _make_funding_company("WThinCo")
    db.add(thin)
    await db.flush()
    db.add(RawPage(company_id=thin.id, url=f"{thin.website}about", content="hi"))

    rich = [_make_funding_company(f"WRichCo{i}") for i in range(2)]
    db.add_all(rich)
    await db.flush()
    for c in rich:
        db.add(_add_raw_page(c.id, f"{c.website}about"))
    await db.flush()
    await db.commit()

    calls: list[str] = []

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        calls.append(prompt)
        return _make_extraction(is_funding=False, leads=[], others=[])

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding_website(db, limit=100, concurrency=5)
    assert summary.companies_seen == 3
    assert summary.skipped_no_text == 1
    # Only the two rich companies hit the LLM.
    assert len(calls) == 2

    await db.refresh(thin)
    assert thin.website_funding_checked_at is not None


# ---------------------------------------------------------------------------
# News-article-path bounded concurrency (parity with the sequential behavior)
# ---------------------------------------------------------------------------


def _extraction_for_news_prompt(prompt: str) -> FundingExtraction:
    """Route a stubbed extraction off the company name embedded in the prompt,
    so a concurrent batch and a sequential run see the SAME deterministic
    mapping for a given fixture. Exercises every Phase-3 branch:

    - ``*FundCo*``  -> a high-confidence funding round (rounds + investors)
    - ``*NotCo*``   -> not a funding announcement (terminal skip, processed)
    - ``*LowCo*``   -> a low-confidence round (transient skip, NOT processed)
    - ``*AcqCo*``   -> a non-funding acquisition with a stated total
    - anything else -> a plain high-confidence round

    Investor names are namespaced by the fixture tag ('seq'/'par') embedded in
    the company name so the two runs create DISJOINT investor rows — otherwise
    the second run's upsert_investor would dedupe onto the first run's rows and
    its investors_created counter would diverge purely from shared global state.
    """
    tag = "seq" if "seq" in prompt else "par"
    if "NotCo" in prompt:
        return _make_extraction(is_funding=False, leads=[], others=[])
    if "LowCo" in prompt:
        return _make_extraction(confidence="low", leads=[f"{tag} Lightspeed"], others=[])
    if "AcqCo" in prompt:
        return _make_extraction(
            is_funding=False,
            leads=[],
            others=[],
            status_event="acquired",
            status_confidence="high",
            total_raised=Decimal("120000000.00"),
        )
    return _make_extraction(
        round_type="Series A",
        amount=Decimal("50000000.00"),
        valuation=None,
        leads=[f"{tag} Lightspeed"],
        others=[f"{tag} Founders Fund"],
        confidence="high",
    )


async def _seed_news_mix(db: AsyncSession, *, tag: str) -> dict[str, Any]:
    """Create one company + one unprocessed article for each Phase-3 branch,
    namespaced by ``tag`` so two independent runs don't collide. Returns the
    company/article ids by kind for cross-run comparison."""
    kinds = ["FundCo", "NotCo", "LowCo", "AcqCo"]
    ids: dict[str, Any] = {}
    for kind in kinds:
        company = _make_company(f"{tag}{kind}")
        db.add(company)
        await db.flush()
        article = _make_article(
            company.id,
            url=f"https://news.example.com/{tag}-{kind}",
            raw_content=f"{tag}{kind} coverage body. " * 20,
            published=date(2026, 5, 1),
        )
        db.add(article)
        await db.flush()
        ids[kind] = {"company_id": company.id, "article_id": article.id}
    await db.commit()
    return ids


async def _snapshot_news_state(
    db: AsyncSession, ids: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Capture the per-kind outcome (company status/total, round count + first
    round's primary_news_url, article processed flag) into plain values so two
    independent runs can be compared after the fact."""
    snap: dict[str, dict[str, Any]] = {}
    for kind, entry in ids.items():
        company = await db.get(Company, entry["company_id"])
        article = await db.get(NewsArticle, entry["article_id"])
        assert company is not None and article is not None
        rounds = (
            await db.execute(
                select(FundingRound)
                .where(FundingRound.company_id == company.id)
                .order_by(FundingRound.announced_date.asc())
            )
        ).scalars().all()
        snap[kind] = {
            "status": company.status,
            "total_raised_usd": company.total_raised_usd,
            "round_count": len(rounds),
            "round_url": rounds[0].primary_news_url if rounds else None,
            "processed": article.processed,
        }
    return snap


async def test_news_concurrency_matches_sequential_state_and_counts(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrency=5 run produces the same DB state (rounds, status, totals,
    processed flags) AND the same summary counters as a concurrency=1
    (sequential) run over an identical fixture.

    Two disjoint fixtures (tags 'seq' and 'par') are run independently. Because
    the news queue is global (``processed=false``), the seq run's intentional
    leftover (its low-confidence article) is neutralized BEFORE the par run so
    the par run only touches its own fixture — the parity claim is about the
    two runs in isolation, not about queue interaction."""

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        return _extraction_for_news_prompt(prompt)

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    # --- Sequential reference run -------------------------------------------
    seq_ids = await _seed_news_mix(db, tag="seq")
    seq = await run_extract_funding(db, limit=100, concurrency=1)
    seq_snap = await _snapshot_news_state(db, seq_ids)

    # Neutralize seq's intentional leftover (low-confidence → processed=false)
    # so the global queue is empty before the par fixture is introduced.
    leftover = await db.get(NewsArticle, seq_ids["LowCo"]["article_id"])
    assert leftover is not None and leftover.processed is False
    leftover.processed = True
    db.add(leftover)
    await db.commit()

    # --- Concurrent run over an identical fresh fixture ----------------------
    par_ids = await _seed_news_mix(db, tag="par")
    par = await run_extract_funding(db, limit=100, concurrency=5)
    par_snap = await _snapshot_news_state(db, par_ids)

    # Identical summary counters across the two runs.
    assert seq.model_dump() == par.model_dump()
    # Sanity: the fixture actually exercised the interesting branches.
    assert seq.funding_rounds_created == 1  # FundCo
    assert seq.investors_created == 2  # FundCo's two namespaced investors
    assert seq.investor_links_created == 2
    assert seq.skipped_not_funding == 2  # NotCo + AcqCo (both is_funding=False)
    assert seq.skipped_low_confidence == 1  # LowCo
    assert seq.status_changes_applied == 1  # AcqCo
    assert seq.totals_recorded == 1  # AcqCo
    # articles_processed increments for EVERY article past the LLM call (before
    # the funding/low-conf gates), so all four count — Low included.
    assert seq.articles_processed == 4

    # Per-kind DB-state parity (normalize the per-run URL/namespace difference).
    for kind in ("FundCo", "NotCo", "LowCo", "AcqCo"):
        s, p = seq_snap[kind], par_snap[kind]
        assert s["status"] == p["status"]
        assert s["total_raised_usd"] == p["total_raised_usd"]
        assert s["round_count"] == p["round_count"]
        assert s["processed"] == p["processed"]

    # Concrete expectations per kind (so the parity check can't pass vacuously).
    assert seq_snap["LowCo"]["processed"] is False  # transient skip
    assert par_snap["LowCo"]["processed"] is False
    assert seq_snap["FundCo"]["processed"] is True
    assert seq_snap["FundCo"]["round_count"] == 1
    assert seq_snap["AcqCo"]["status"] == "acquired"
    assert par_snap["AcqCo"]["status"] == "acquired"
    # Each round is attributed to its own article URL (write attribution intact).
    assert par_snap["FundCo"]["round_url"] == "https://news.example.com/par-FundCo"


async def test_news_concurrency_runs_llm_calls_in_parallel(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The news-funding LLM calls for a batch overlap in time (bounded by the
    semaphore). Proven by recording the peak concurrently-in-flight calls."""
    n = 8
    companies = [_make_company(f"NParCo{i:02d}") for i in range(n)]
    db.add_all(companies)
    await db.flush()
    for c in companies:
        db.add(_make_article(c.id, url=f"https://news.example.com/npar-{c.id}"))
    await db.flush()
    await db.commit()

    state = {"in_flight": 0, "peak": 0}

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            await asyncio.sleep(0.02)
            return _make_extraction(is_funding=False, leads=[], others=[])
        finally:
            state["in_flight"] -= 1

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    await run_extract_funding(db, limit=100, concurrency=4)
    assert state["peak"] >= 2
    assert state["peak"] <= 4  # never exceeds the semaphore bound


async def test_news_concurrency_one_is_strictly_sequential(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """concurrency=1 degrades to one-at-a-time: peak in-flight is never > 1."""
    n = 5
    companies = [_make_company(f"NSeqCo{i}") for i in range(n)]
    db.add_all(companies)
    await db.flush()
    for c in companies:
        db.add(_make_article(c.id, url=f"https://news.example.com/nseq-{c.id}"))
    await db.flush()
    await db.commit()

    state = {"in_flight": 0, "peak": 0}

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            await asyncio.sleep(0.005)
            return _make_extraction(is_funding=False, leads=[], others=[])
        finally:
            state["in_flight"] -= 1

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding(db, limit=100, concurrency=1)
    assert summary.articles_processed == n
    assert state["peak"] == 1


async def test_news_concurrency_rate_limit_stops_scheduling_no_stamp(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 stops scheduling further batches; the rate-limited article is NOT
    marked processed (so it stays eligible next run) and later batches never hit
    the LLM, while first-batch articles are processed.

    Articles are selected newest published_date first; giving each a distinct
    descending date makes the order deterministic. With concurrency 3 the
    batches are [00,01,02] [03,04,05] [06,07,08] [09]; the 429 fires on index 4
    (second batch), so the third+ batches are never scheduled.
    """
    n = 10
    article_ids: list[Any] = []
    for i in range(n):
        company = _make_company(f"NRlCo{i:02d}")
        db.add(company)
        await db.flush()
        article = _make_article(
            company.id,
            url=f"https://news.example.com/nrl-{i:02d}",
            # Strictly descending dates → selection order is index 00,01,...,09.
            published=date(2026, 5, 1) - timedelta(days=i),
        )
        db.add(article)
        await db.flush()
        article_ids.append(article.id)
    await db.commit()
    rate_limited_id = article_ids[4]

    seen: list[str] = []

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        for i in range(n):
            if f"NRlCo{i:02d}" in prompt:
                seen.append(f"NRlCo{i:02d}")
                if i == 4:
                    raise LLMRateLimitError("429")
                return _make_extraction(is_funding=False, leads=[], others=[])
        raise AssertionError("unknown company in prompt")

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding(db, limit=100, concurrency=3)

    assert summary.skipped_rate_limited == 1
    # Third+ batches never reached the LLM.
    for i in range(6, n):
        assert f"NRlCo{i:02d}" not in seen

    # First-batch articles completed and were marked processed.
    for i in range(3):
        a = await db.get(NewsArticle, article_ids[i])
        assert a is not None
        assert a.processed is True

    # The rate-limited article is deliberately left unprocessed — it keeps its
    # slot for the next run.
    rl = await db.get(NewsArticle, rate_limited_id)
    assert rl is not None
    assert rl.processed is False


async def test_news_concurrency_low_confidence_left_unprocessed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under concurrency, a low-confidence round is still a transient skip: the
    article stays processed=false and no round is created (matches the serial
    behavior), alongside a high-confidence article that DOES land a round."""
    low = _make_company("NLowCo")
    high = _make_company("NHighCo")
    db.add_all([low, high])
    await db.flush()
    low_article = _make_article(low.id, url="https://news.example.com/nlow")
    high_article = _make_article(high.id, url="https://news.example.com/nhigh")
    db.add_all([low_article, high_article])
    await db.flush()
    await db.commit()
    low_article_id = low_article.id

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        if "NLowCo" in prompt:
            return _make_extraction(confidence="low", leads=["Lightspeed"], others=[])
        return _make_extraction(leads=["Sequoia"], others=[])

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding(db, limit=100, concurrency=5)
    assert summary.skipped_low_confidence == 1
    assert summary.funding_rounds_created == 1  # only the high-confidence one

    refetched = await db.get(NewsArticle, low_article_id)
    assert refetched is not None
    assert refetched.processed is False

    low_rounds = (
        await db.execute(select(FundingRound).where(FundingRound.company_id == low.id))
    ).scalars().all()
    assert len(low_rounds) == 0


async def test_news_concurrency_llm_failure_skips_and_leaves_unprocessed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parse/other LLM error on one article is counted under llm_failures and
    leaves that article unprocessed for retry, WITHOUT stopping the batch (only
    a 429 stops scheduling). Other articles in the batch still succeed."""
    from nous.llm.client import LLMParseError

    bad = _make_company("NBadCo")
    good = _make_company("NGoodCo")
    db.add_all([bad, good])
    await db.flush()
    bad_article = _make_article(bad.id, url="https://news.example.com/nbad")
    good_article = _make_article(good.id, url="https://news.example.com/ngood")
    db.add_all([bad_article, good_article])
    await db.flush()
    await db.commit()
    bad_article_id = bad_article.id
    good_article_id = good_article.id

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        if "NBadCo" in prompt:
            raise LLMParseError("could not parse")
        return _make_extraction(leads=["Sequoia"], others=[])

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding(db, limit=100, concurrency=5)
    assert summary.llm_failures == 1
    assert summary.skipped_rate_limited == 0  # a parse error does NOT stop the run
    assert summary.funding_rounds_created == 1  # the good article still lands

    bad_refetched = await db.get(NewsArticle, bad_article_id)
    assert bad_refetched is not None
    assert bad_refetched.processed is False  # retryable
    good_refetched = await db.get(NewsArticle, good_article_id)
    assert good_refetched is not None
    assert good_refetched.processed is True


async def test_news_concurrency_preserves_deterministic_write_order(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two articles about the SAME round (within the proximity window) reconcile
    into ONE FundingRound whose primary_news_url is first-write-wins. Phase 3
    applies writes in selection order (newest published_date first), so the
    newer article's URL must win — proving the concurrent path preserves the
    serial loop's deterministic write ordering, not whichever LLM call returned
    first."""
    company = _make_company("OrderCo")
    db.add(company)
    await db.flush()
    # Newer article is selected first (published_date desc). Same round_type +
    # dates within ±60 days → they reconcile into one round.
    newer = _make_article(
        company.id,
        url="https://news.example.com/order-newer",
        published=date(2026, 5, 20),
    )
    older = _make_article(
        company.id,
        url="https://news.example.com/order-older",
        published=date(2026, 5, 1),
    )
    db.add_all([newer, older])
    await db.flush()
    await db.commit()

    async def _fake(prompt: str, schema: type) -> FundingExtraction:
        # Same round_type for both; announced date inside the proximity window.
        return _make_extraction(
            round_type="Series A",
            amount=Decimal("50000000.00"),
            valuation=None,
            announced=date(2026, 5, 10),
            leads=["Lightspeed"],
            others=[],
            confidence="high",
        )

    monkeypatch.setattr("nous.pipeline.extract_funding.complete_json", _fake)

    summary = await run_extract_funding(db, limit=100, concurrency=5)
    assert summary.funding_rounds_created == 1
    assert summary.funding_rounds_merged == 1

    rounds = (
        await db.execute(select(FundingRound).where(FundingRound.company_id == company.id))
    ).scalars().all()
    assert len(rounds) == 1
    # The newer article is processed first, so it creates the round and owns
    # primary_news_url (first-write-wins). Order-independent code would flake.
    assert rounds[0].primary_news_url == "https://news.example.com/order-newer"


# ---------------------------------------------------------------------------
# Google-News redirect resolution for primary_news_url
#
# When ingest could not de-redirect a Google-News link (consent interstitial,
# robots-block, paywall stub, fetch error, or a legacy pre-Task-A1 row), the
# article row still holds a ``news.google.com/...`` redirect. Extract-funding
# must NOT copy that opaque redirect into ``funding_rounds.primary_news_url``
# (the web "Sources" section would then link to news.google.com instead of the
# real publisher). It resolves the redirect to the publisher URL just before
# storing, falling back to the original only if resolution fails.
# ---------------------------------------------------------------------------


class _FakeResolver:
    """Stand-in NewsRedirectResolver. Records calls; returns a canned result."""

    def __init__(self, mapping: dict[str, ResolvedArticle | None]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def resolve_article(self, url: str) -> ResolvedArticle | None:
        self.calls.append(url)
        return self._mapping.get(url)


_GN_REDIRECT = "https://news.google.com/rss/articles/CBMiAbCdEf?oc=5"


async def test_google_news_source_url_resolved_to_publisher(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A round sourced from an unresolved GN redirect stores the PUBLISHER URL."""
    company = _make_company("RedirectCo")
    db.add(company)
    await db.flush()
    db.add(
        _make_article(
            company.id,
            url=_GN_REDIRECT,
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

    publisher = "https://www.reuters.com/tech/redirectco-raises-50m"
    resolver = _FakeResolver(
        {
            _GN_REDIRECT: ResolvedArticle(
                url=publisher,
                source="reuters.com",
                body="x" * 600,
            )
        }
    )

    summary = await run_extract_funding(db, limit=10, resolver=resolver)
    assert summary.funding_rounds_created == 1

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    # The Google-News redirect was de-referenced to the real publisher URL.
    assert rounds[0].primary_news_url == publisher
    # The resolver was consulted exactly once, with the GN redirect.
    assert resolver.calls == [_GN_REDIRECT]


async def test_google_news_source_url_unresolvable_keeps_original(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the GN redirect can't be resolved, keep it — better than no source."""
    company = _make_company("UnresolvableCo")
    db.add(company)
    await db.flush()
    db.add(
        _make_article(company.id, url=_GN_REDIRECT, published=date(2026, 5, 1))
    )
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction()

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    # Resolver returns None (consent interstitial / robots-block / paywall).
    resolver = _FakeResolver({_GN_REDIRECT: None})

    await run_extract_funding(db, limit=10, resolver=resolver)

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].primary_news_url == _GN_REDIRECT
    assert resolver.calls == [_GN_REDIRECT]


async def test_publisher_source_url_is_not_resolved(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-resolved publisher URL is stored as-is; resolver untouched.

    This is the common path (ingest resolves at Task A1), so it must incur no
    resolver call at all — the round's source URL passes through unchanged.
    """
    company = _make_company("PublisherCo")
    db.add(company)
    await db.flush()
    publisher = "https://techcrunch.com/2026/05/01/publisherco-series-a"
    db.add(_make_article(company.id, url=publisher, published=date(2026, 5, 1)))
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction()

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    # Map the GN redirect only; a publisher URL must never reach the resolver.
    resolver = _FakeResolver({_GN_REDIRECT: None})

    await run_extract_funding(db, limit=10, resolver=resolver)

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].primary_news_url == publisher
    assert resolver.calls == []  # non-GN URL → no resolution attempted


async def test_resolver_none_disables_resolution(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolver=None`` stores the source URL verbatim (resolution disabled)."""
    company = _make_company("DisabledCo")
    db.add(company)
    await db.flush()
    db.add(
        _make_article(company.id, url=_GN_REDIRECT, published=date(2026, 5, 1))
    )
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction()

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    await run_extract_funding(db, limit=10, resolver=None)

    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].primary_news_url == _GN_REDIRECT


async def test_resolver_failure_falls_back_to_redirect(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raised resolver error must not break extraction; keep the redirect."""
    company = _make_company("BoomCo")
    db.add(company)
    await db.flush()
    db.add(
        _make_article(company.id, url=_GN_REDIRECT, published=date(2026, 5, 1))
    )
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> FundingExtraction:
        return _make_extraction()

    monkeypatch.setattr(
        "nous.pipeline.extract_funding.complete_json", _fake_complete_json
    )

    class _BoomResolver:
        async def resolve_article(self, url: str) -> ResolvedArticle | None:
            raise RuntimeError("network exploded")

    summary = await run_extract_funding(db, limit=10, resolver=_BoomResolver())
    # Extraction still succeeded; the round just keeps the redirect URL.
    assert summary.funding_rounds_created == 1
    rounds = (await db.execute(select(FundingRound))).scalars().all()
    assert rounds[0].primary_news_url == _GN_REDIRECT
