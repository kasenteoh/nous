"""Integration tests for the unexclude-prominent backfill lever.

Requires DATABASE_URL (same gating as the other DB suites). Exercises the
selection precision (only not_a_startup + prominent rows), the clear on --apply,
idempotency, and the operator-intent guard (manual/non_us untouched).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound
from nous.pipeline.unexclude_prominent import run_unexclude_prominent

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _excluded_co(slug: str, *, reason: str, detail: str | None = None) -> Company:
    return Company(
        name=slug,
        slug=slug,
        normalized_name=slug,
        hq_country="US",
        exclusion_reason=reason,
        exclusion_detail=detail,
        excluded_at=datetime.now(tz=UTC),
    )


async def _add_round(db: AsyncSession, company_id: object, amount: Decimal) -> None:
    db.add(FundingRound(company_id=company_id, amount_raised=amount))
    await db.flush()


async def test_dry_run_selects_only_prominent_not_a_startup(
    db: AsyncSession,
) -> None:
    # Prominent not_a_startup — the target.
    hit = _excluded_co(
        "unexcl-hit", reason="not_a_startup", detail="Mature aerospace co."
    )
    # not_a_startup but below threshold — must NOT match.
    small = _excluded_co("unexcl-small", reason="not_a_startup")
    # Prominent but excluded for a DIFFERENT (operator/geo) reason — must NOT
    # match: operator intent wins.
    non_us = _excluded_co("unexcl-nonus", reason="non_us")
    manual = _excluded_co("unexcl-manual", reason="manual")
    db.add_all([hit, small, non_us, manual])
    await db.flush()
    await _add_round(db, hit.id, Decimal("650000000"))
    await _add_round(db, small.id, Decimal("10000000"))
    await _add_round(db, non_us.id, Decimal("900000000"))
    await _add_round(db, manual.id, Decimal("800000000"))
    await db.commit()

    summary = await run_unexclude_prominent(db, dry_run=True)
    assert summary.dry_run is True
    assert summary.candidates == 1
    assert summary.cleared == 0
    assert [r.slug for r in summary.companies] == ["unexcl-hit"]
    assert summary.companies[0].max_round_usd == "$650,000,000"
    assert summary.companies[0].exclusion_detail == "Mature aerospace co."

    # Dry-run wrote nothing: the row is still excluded.
    await db.refresh(hit)
    assert hit.exclusion_reason == "not_a_startup"


async def test_apply_clears_and_is_idempotent(db: AsyncSession) -> None:
    hit = _excluded_co("unexcl-apply", reason="not_a_startup", detail="was junk")
    db.add(hit)
    await db.flush()
    await _add_round(db, hit.id, Decimal("500000000"))
    await db.commit()

    summary = await run_unexclude_prominent(db, dry_run=False)
    assert summary.dry_run is False
    assert summary.candidates == 1
    assert summary.cleared == 1

    await db.refresh(hit)
    assert hit.exclusion_reason is None
    assert hit.exclusion_detail is None
    assert hit.excluded_at is None

    # Second run selects nothing — the cleared row no longer matches.
    summary2 = await run_unexclude_prominent(db, dry_run=False)
    assert summary2.candidates == 0
    assert summary2.cleared == 0


async def test_apply_leaves_non_us_and_manual_untouched(db: AsyncSession) -> None:
    non_us = _excluded_co("unexcl-keep-nonus", reason="non_us")
    manual = _excluded_co("unexcl-keep-manual", reason="manual")
    db.add_all([non_us, manual])
    await db.flush()
    await _add_round(db, non_us.id, Decimal("900000000"))
    await _add_round(db, manual.id, Decimal("800000000"))
    await db.commit()

    summary = await run_unexclude_prominent(db, dry_run=False)
    assert summary.candidates == 0
    assert summary.cleared == 0

    await db.refresh(non_us)
    await db.refresh(manual)
    assert non_us.exclusion_reason == "non_us"
    assert manual.exclusion_reason == "manual"
