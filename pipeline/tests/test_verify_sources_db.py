"""DB-gated integration tests for the source-verification prevalence probe and
the stored-text fact collection.

Requires DATABASE_URL (Postgres with `alembic upgrade head` applied); skipped
otherwise. Seeds companies + funding rounds + news_articles and asserts the
verifiability buckets and the (no-LLM) fact selection. The LLM dry-run itself is
not exercised here — it needs the DeepSeek key (Actions only).
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle
from nous.pipeline.verify_sources import (
    _collect_stored_text_facts,
    run_verify_sources_probe,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_BODY = "TechCrunch reports the round in detail. " * 10  # comfortably ≥ 200 chars


def _company(name: str, **kwargs: object) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{name.lower()}-{suffix}",
        normalized_name=f"{name.lower()}{suffix}",
        description_short="A shown company.",  # shown regardless of funding
        **kwargs,  # type: ignore[arg-type]
    )


def _news(company_id: object, url: str) -> NewsArticle:
    return NewsArticle(
        company_id=company_id,
        url=url,
        title="Round announced",
        source="techcrunch.com",
        raw_content=_BODY,
    )


async def test_probe_buckets_and_fact_collection(db: AsyncSession) -> None:
    tc_a = "https://techcrunch.com/acme-series-a"
    tc_e = "https://techcrunch.com/echo-acquired"
    gnews = "https://news.google.com/rss/articles/CBMiOpaqueRedirect"
    reuters = "https://reuters.com/charlie-round"

    # A — shown; total_raised + a funding round both cite a STORED news article.
    a = _company(
        "Alpha",
        latest_round_amount=Decimal("300000000"),
        total_raised_usd=Decimal("12000000"),
        total_raised_source_url=tc_a,
    )
    # B — shown; a funding round cites a bare Google News redirect (no stored text).
    b = _company("Bravo", latest_round_amount=Decimal("200000000"))
    # C — shown; a funding round cites a real host with NO stored text → refetch.
    c = _company("Charlie", latest_round_amount=Decimal("150000000"))
    # D — EXCLUDED: sourced facts here must be counted nowhere.
    d = _company(
        "Delta",
        exclusion_reason="non_us",
        total_raised_usd=Decimal("900000000"),
        total_raised_source_url=tc_a,
        status="acquired",
        status_source_url=tc_e,
    )
    # E — shown; a non-active status cites a STORED news article.
    e = _company(
        "Echo",
        latest_round_amount=Decimal("500000000"),
        status="acquired",
        status_source_url=tc_e,
    )
    for co in (a, b, c, d, e):
        db.add(co)
    await db.flush()

    db.add(_news(a.id, tc_a))
    db.add(_news(e.id, tc_e))
    db.add(FundingRound(company_id=a.id, primary_news_url=tc_a, amount_raised=Decimal("12000000")))
    db.add(FundingRound(company_id=b.id, primary_news_url=gnews))
    db.add(FundingRound(company_id=c.id, primary_news_url=reuters))
    db.add(FundingRound(company_id=d.id, primary_news_url=tc_a))  # excluded → ignored
    await db.flush()

    summary = await run_verify_sources_probe(db)

    # total_raised: A stored (D excluded).
    assert summary.buckets_by_kind["total_raised"]["stored"] == 1
    assert summary.facts_by_kind["total_raised"] == 1
    # status: E stored (D excluded).
    assert summary.buckets_by_kind["status"]["stored"] == 1
    assert summary.facts_by_kind["status"] == 1
    # funding_round: A stored, B unreachable, C refetch (D excluded).
    fr = summary.buckets_by_kind["funding_round"]
    assert fr["stored"] == 1
    assert fr["unreachable"] == 1
    assert fr["refetch"] == 1
    assert summary.facts_by_kind["funding_round"] == 3

    # Aggregates: 3 stored, 1 refetch, 1 unreachable, 0 unparseable.
    assert summary.stored == 3
    assert summary.refetch == 1
    assert summary.unreachable == 1
    assert summary.unparseable == 0
    assert summary.total_facts == 5
    assert summary.addressable == 4

    # Fact collection returns exactly the 3 stored-text facts, each with a claim
    # and loaded source text (no LLM).
    facts = await _collect_stored_text_facts(db, limit=10)
    kinds = sorted((f.company_name, f.fact_kind) for f in facts)
    assert kinds == [
        ("Alpha", "funding_round"),
        ("Alpha", "total_raised"),
        ("Echo", "status"),
    ]
    for f in facts:
        assert f.claim  # a non-empty claim string
        assert len(f.source_text) >= 200  # stored body loaded and truncated


async def test_probe_empty_cohort_is_zeroed(db: AsyncSession) -> None:
    # No sourced facts at all → all counts zero, no division-by-zero.
    summary = await run_verify_sources_probe(db)
    assert summary.total_facts == 0
    assert summary.addressable_pct == 0.0
    assert summary.stored_pct == 0.0
