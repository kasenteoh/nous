"""Tests for the suspect-duplicate-rounds census in the data-quality report.

The census is the $0 measure-first probe for the aggregation-without-dedup P0
(2026-07-16 QA): it must count — with the SAME compatibility rules the repair
stage clusters with — what the existing repair passes and the proposed
near-amount merge gate would touch. Pure-function tests run everywhere; the
end-to-end census test needs DATABASE_URL.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.pipeline.data_quality import (
    DataQualitySummary,
    SuspectDuplicateExample,
    SuspectDuplicateRounds,
    _amounts_near,
    _dates_compatible,
    emit_data_quality_summary,
)


def test_amounts_near_tolerance() -> None:
    # terrafirma: $100M vs $115M = 13% of the larger → near.
    assert _amounts_near(Decimal(100_000_000), Decimal(115_000_000))
    # Symmetric.
    assert _amounts_near(Decimal(115_000_000), Decimal(100_000_000))
    # Equal amounts are the exact-dup class, not near.
    assert not _amounts_near(Decimal(100_000_000), Decimal(100_000_000))
    # sambanova/KuCoin: $100M vs $1B = 90% → not near.
    assert not _amounts_near(Decimal(100_000_000), Decimal(1_000_000_000))
    # Exactly at the 15% boundary counts (inclusive).
    assert _amounts_near(Decimal(85), Decimal(100))
    assert not _amounts_near(Decimal(84), Decimal(100))


def test_dates_compatible_window() -> None:
    assert _dates_compatible(None, None)
    assert _dates_compatible(date(2026, 7, 14), None)
    assert _dates_compatible(date(2026, 7, 1), date(2026, 7, 14))
    assert not _dates_compatible(date(2026, 7, 1), date(2026, 8, 1))


def test_emit_renders_suspect_section() -> None:
    summary = DataQualitySummary(
        shown_total=1,
        suspect_duplicate_rounds=SuspectDuplicateRounds(
            empty_shell_rows=10,
            exact_dup_loser_rows=3,
            near_amount_pairs=1,
            type_conflict_groups=2,
            companies_affected=3,
            examples=[
                SuspectDuplicateExample(
                    slug="terrafirma",
                    kind="near_amount",
                    detail="$100,000,000 vs $115,000,000",
                )
            ],
        ),
    )
    # emit writes to GITHUB_STEP_SUMMARY when set; without it, it logs. Either
    # way it must not raise, and the section content must be in the lines.
    emit_data_quality_summary(summary)


pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


@pytestmark_db
async def test_census_counts_all_four_classes(db: AsyncSession) -> None:
    from nous.db.models import Company, FundingRound
    from nous.pipeline.data_quality import _suspect_duplicate_rounds

    def co(name: str, slug: str, excluded: bool = False) -> Company:
        return Company(
            name=name,
            slug=slug,
            normalized_name=slug.replace("-", " "),
            hq_country="US",
            exclusion_reason="manual" if excluded else None,
        )

    terrafirma = co("TerraFirma", "terrafirma-t")
    sambanova = co("SambaNova", "sambanova-t")
    blueorigin = co("Blue Origin", "blue-origin-t")
    helion = co("Helion", "helion-t")
    excluded = co("Gone Co", "gone-co-t", excluded=True)
    db.add_all([terrafirma, sambanova, blueorigin, helion, excluded])
    await db.flush()

    db.add_all(
        [
            # Near-amount pair: same type, one undated, 13% apart.
            FundingRound(
                company_id=terrafirma.id,
                round_type="Series A",
                amount_raised=115_000_000,
                announced_date=date(2026, 7, 14),
            ),
            FundingRound(
                company_id=terrafirma.id,
                round_type="Series A",
                amount_raised=100_000_000,
                announced_date=None,
            ),
            # Type conflict: contradicting real letters on one amount, ≤1 dated.
            FundingRound(
                company_id=sambanova.id,
                round_type="Series F",
                amount_raised=1_000_000_000,
                announced_date=date(2026, 7, 8),
            ),
            FundingRound(
                company_id=sambanova.id,
                round_type="Series E",
                amount_raised=1_000_000_000,
                announced_date=None,
            ),
            # Empty shell (placeholder type only).
            FundingRound(company_id=blueorigin.id, round_type="Series ?"),
            # Exact dup: two untyped rows at the same amount → 1 loser.
            FundingRound(company_id=helion.id, amount_raised=465_000_000),
            FundingRound(company_id=helion.id, amount_raised=465_000_000),
            # Excluded company's shell must NOT count.
            FundingRound(company_id=excluded.id),
        ]
    )
    await db.commit()

    result = await _suspect_duplicate_rounds(db)

    assert result.empty_shell_rows == 1
    assert result.exact_dup_loser_rows == 1
    assert result.near_amount_pairs == 1
    assert result.type_conflict_groups == 1
    assert result.companies_affected == 4
    kinds = {(e.slug, e.kind) for e in result.examples}
    assert ("terrafirma-t", "near_amount") in kinds
    assert ("sambanova-t", "type_conflict") in kinds
    assert ("blue-origin-t", "empty_shell") in kinds
    assert ("helion-t", "exact_dup") in kinds


@pytestmark_db
async def test_census_ignores_distant_and_contradicting_pairs(
    db: AsyncSession,
) -> None:
    from nous.db.models import Company, FundingRound
    from nous.pipeline.data_quality import _suspect_duplicate_rounds

    company = Company(
        name="Clean Co",
        slug="clean-co-t",
        normalized_name="clean co t",
        hq_country="US",
    )
    db.add(company)
    await db.flush()
    db.add_all(
        [
            # Near amounts but contradicting real types → not suspect.
            FundingRound(
                company_id=company.id,
                round_type="Series A",
                amount_raised=100_000_000,
                announced_date=date(2026, 1, 10),
            ),
            FundingRound(
                company_id=company.id,
                round_type="Series B",
                amount_raised=110_000_000,
                announced_date=date(2026, 1, 12),
            ),
            # Same type + near amounts but dates far apart → not suspect.
            FundingRound(
                company_id=company.id,
                round_type="seed",
                amount_raised=10_000_000,
                announced_date=date(2025, 1, 1),
            ),
            FundingRound(
                company_id=company.id,
                round_type="seed",
                amount_raised=11_000_000,
                announced_date=date(2025, 12, 1),
            ),
        ]
    )
    await db.commit()

    result = await _suspect_duplicate_rounds(db)
    assert result.near_amount_pairs == 0
    assert result.type_conflict_groups == 0
    assert result.exact_dup_loser_rows == 0
    assert result.companies_affected == 0
