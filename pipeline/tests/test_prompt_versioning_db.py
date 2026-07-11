"""DB round-trip tests: the persisting stages stamp prompt versions.

Companion to test_prompt_versioning.py (the pure-unit half). Each test runs a
real stage against Postgres with ``complete_json`` monkeypatched — the same
pattern as test_enrich_companies.py / test_judge_eligibility.py — and asserts
the 0031 provenance stamps land alongside the LLM-derived content.

Requires DATABASE_URL pointing at a live Postgres with the schema at
``alembic upgrade head``; skipped otherwise, like the other DB integration
tests.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nous.db.models import Company, Competitor, RawPage
from nous.db.upsert import reconcile_funding_round
from nous.llm.prompts import (
    company_description,
    company_description_long,
    company_eligibility,
    competitor_analysis,
    funding_extraction,
)
from nous.llm.prompts.company_description import CompanyDescription
from nous.llm.prompts.company_description_long import CompanyLongDescription
from nous.llm.prompts.company_eligibility import EligibilityJudgment
from nous.llm.prompts.competitor_analysis import (
    Competitor as CompetitorOut,
)
from nous.llm.prompts.competitor_analysis import CompetitorAnalysis
from nous.llm.prompts.funding_extraction import FundingExtraction
from nous.pipeline.analyze_competitors import run_analyze_competitors
from nous.pipeline.enrich_companies import run_enrich_companies
from nous.pipeline.judge_eligibility import run_judge_eligibility

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

Factory = async_sessionmaker[AsyncSession]

# A page whose visible text clears enrich's _MIN_TEXT_CHARS (200) bar AND
# the _MIN_DESCRIBE_CHARS (700) bar, so the stage runs the full two-call
# judge + describe flow.
_SUBSTANTIAL_PAGE = (
    "<html><body><p>This is a substantial enough page to pass the minimum "
    "text check. The company builds developer tools for API-first teams. "
    "Their platform enables engineers to design, test, and deploy APIs at "
    "scale. Founded in 2021, they serve hundreds of enterprise customers "
    "globally. Their flagship product is a cloud-native API gateway with "
    "built-in observability. The gateway terminates traffic close to users "
    "and applies rate limits, authentication, and schema validation before "
    "requests reach upstream services. A control plane manages configuration "
    "as code, with previews for every change and automatic rollback when "
    "error rates rise. Customers integrate through declarative manifests "
    "checked into their own repositories, and usage-based pricing scales "
    "from side projects to large enterprise deployments.</p></body></html>"
)


def _make_company(name: str, *, slug_prefix: str, **kwargs: object) -> Company:
    # Random slug suffix keeps fixtures from colliding on the unique slug
    # across reruns (same convention as the other DB integration tests).
    defaults: dict[str, object] = {
        "name": name,
        "slug": f"{slug_prefix}-{os.urandom(4).hex()}",
        "normalized_name": name.lower(),
        "hq_country": "US",
    }
    defaults.update(kwargs)
    return Company(**defaults)


async def test_enrich_stamps_enrichment_and_eligibility_versions(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-call enrich (W-F): eligibility carries the JUDGE prompt's version
    (its output holds the judgment), enrichment carries the DESCRIBE
    prompt's version (it owns description_long state)."""
    company = _make_company("Stampable Inc", slug_prefix="pv-enrich")
    db.add(company)
    await db.flush()
    db.add(
        RawPage(
            company_id=company.id,
            url="https://stampable.example/",
            content=_SUBSTANTIAL_PAGE,
        )
    )
    await db.commit()

    canned = CompanyDescription(
        description_short="Builds API tools.",
        primary_category="developer tools",
        tags=["api"],
        website_state="ok",
    )
    canned_long = CompanyLongDescription(
        description_long="Builds API tools for teams.\n\nUsed by platform teams."
    )

    async def _route(prompt: str, schema: type, **kwargs: object) -> object:
        if schema is CompanyDescription:
            return canned
        assert schema is CompanyLongDescription
        return canned_long

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        _route,
    )

    summary = await run_enrich_companies(db)
    assert summary.companies_enriched >= 1

    await db.refresh(company)
    assert (
        company.enrichment_prompt_version
        == company_description_long.PROMPT_VERSION
    )
    assert (
        company.eligibility_prompt_version
        == company_description.PROMPT_VERSION
    )


async def test_judge_stamps_eligibility_version(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backfill path stamps the company_eligibility prompt's version."""
    async with committed_session_factory() as s1:
        company = _make_company(
            "Old Directory",
            slug_prefix="pv-judge",
            description_short="Does things.",
            description_long="Does many things.",
            last_enriched_at=datetime.now(tz=UTC),
        )
        s1.add(company)
        await s1.flush()
        company_id: UUID = company.id
        await s1.commit()

    canned = EligibilityJudgment(
        is_startup=False,
        not_startup_reason="Founded in 2000; publicly traded.",
    )
    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_judge_eligibility(committed_session_factory)
    assert summary.companies_judged == 1

    async with committed_session_factory() as s3:
        refetched = await s3.get(Company, company_id)
    assert refetched is not None
    assert (
        refetched.eligibility_prompt_version
        == company_eligibility.PROMPT_VERSION
    )


async def test_analyze_competitors_stamps_rows(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with committed_session_factory() as s1:
        target = _make_company(
            "PvTarget",
            slug_prefix="pv-comp",
            description_short="Target short.",
            description_long="Target long description.",
            industry_group="SaaS",
        )
        s1.add(target)
        await s1.flush()
        target_id: UUID = target.id
        await s1.commit()

    async def _fake_complete_json(
        prompt: str, schema: type[CompetitorAnalysis]
    ) -> CompetitorAnalysis:
        assert schema is CompetitorAnalysis
        return CompetitorAnalysis(
            competitors=[
                CompetitorOut(
                    name="RivalCo",
                    description="A rival product.",
                    reasoning="Same market and buyer.",
                    rank=1,
                )
            ]
        )

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json",
        _fake_complete_json,
    )

    async with committed_session_factory() as s2:
        summary = await run_analyze_competitors(s2, limit=5)
    assert summary.competitors_written >= 1

    async with committed_session_factory() as s3:
        rows = (
            (
                await s3.execute(
                    select(Competitor).where(
                        Competitor.company_id == target_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows
    for row in rows:
        assert row.prompt_version == competitor_analysis.PROMPT_VERSION


async def test_reconcile_funding_round_stamps_insert_and_merge(
    db: AsyncSession,
) -> None:
    company = _make_company("Rounded Inc", slug_prefix="pv-round")
    db.add(company)
    await db.flush()

    extraction = FundingExtraction(
        is_funding_announcement=True,
        round_type="Series A",
        amount_raised_usd=Decimal("50000000"),
        confidence="high",
    )
    row, created = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=extraction,
        primary_news_url="https://news.example/a",
    )
    assert created is True
    assert row.prompt_version == funding_extraction.PROMPT_VERSION

    # Merging a re-extraction into the same round keeps the stamp current.
    row.prompt_version = None  # simulate a pre-versioning row
    await db.flush()
    merged, created2 = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=extraction,
        primary_news_url="https://news.example/b",
    )
    assert created2 is False
    assert merged.id == row.id
    assert merged.prompt_version == funding_extraction.PROMPT_VERSION
