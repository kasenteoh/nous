"""Integration tests for the judge-eligibility backfill stage.

complete_json is monkeypatched; requires DATABASE_URL (same gating as the
other DB suites).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.llm.prompts.company_eligibility import EligibilityJudgment
from nous.pipeline.judge_eligibility import run_judge_eligibility

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _enriched_company(name: str, slug: str) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        description_short="Does things.",
        description_long="Does many things.",
        last_enriched_at=datetime.now(tz=UTC),
    )


async def test_judgment_excludes_and_stamps(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_co = _enriched_company("Old Enterprise", "old-enterprise-judge")
    db.add(old_co)
    await db.flush()
    db.add(
        RawPage(
            company_id=old_co.id,
            url="https://old.example/",
            content="Serving the enterprise since 2000." * 20,
        )
    )
    await db.commit()

    canned = EligibilityJudgment(
        is_startup=False,
        not_startup_reason="Founded in 2000; publicly traded.",
        founded_year=2000,
    )
    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_judge_eligibility(db)
    assert summary.companies_judged == 1
    assert summary.companies_excluded == 1

    await db.refresh(old_co)
    assert old_co.exclusion_reason == "not_a_startup"
    assert old_co.eligibility_checked_at is not None
    assert old_co.year_incorporated == 2000

    # Second run selects nothing — the stamp makes the backfill one-shot.
    summary2 = await run_judge_eligibility(db)
    assert summary2.companies_judged == 0


async def test_unknown_keeps_company(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co = _enriched_company("Fine Co", "fine-co-judge")
    db.add(co)
    await db.commit()

    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=EligibilityJudgment()),
    )

    summary = await run_judge_eligibility(db)
    assert summary.companies_judged == 1
    assert summary.companies_excluded == 0
    await db.refresh(co)
    assert co.exclusion_reason is None
    assert co.eligibility_checked_at is not None


async def test_non_us_judgment_excludes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The country judgment is half the reason this stage exists; exercise it
    # directly rather than relying on parity with the enrich path.
    co = _enriched_company("Bangalore Co", "bangalore-co-judge")
    db.add(co)
    await db.commit()

    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(
            return_value=EligibilityJudgment(is_startup=True, hq_country="IN")
        ),
    )

    summary = await run_judge_eligibility(db)
    assert summary.companies_excluded == 1
    await db.refresh(co)
    assert co.exclusion_reason == "non_us"
    assert co.hq_country == "IN"
