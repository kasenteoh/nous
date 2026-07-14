"""DB-gated integration tests for the career-history feasibility probe.

Requires DATABASE_URL (Postgres with `alembic upgrade head` applied); skipped
otherwise. Seeds a handful of companies + raw_pages and asserts the read-only
SQL aggregate counts and the prominence-sample block.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.pipeline.career_history_probe import run_career_history_probe

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _company(name: str, **kwargs: object) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{name.lower()}-{suffix}",
        normalized_name=f"{name.lower()}{suffix}",
        **kwargs,  # type: ignore[arg-type]
    )


async def test_probe_counts_and_sample(db: AsyncSession) -> None:
    # A — shown, top-funded, bio + Tier-1 (previously) + Tier-2 (Stripe).
    a = _company("Alpha", latest_round_amount=Decimal("300000000"))
    # C — shown, second-funded, bio (the team) + Tier-1 (formerly) + Tier-2 (Google).
    c = _company("Charlie", latest_round_amount=Decimal("200000000"))
    # B — shown, no funding, NO bio / NO cue: the clean negative control. It is
    # cue-less so ``~*`` (which can't enforce the capital heuristic) still can't
    # match it, keeping the SQL assertions exact.
    b = _company("Bravo", latest_round_amount=None)
    # D — EXCLUDED: has ex-Amazon signal but must be counted nowhere.
    d = _company("Delta", latest_round_amount=Decimal("900000000"), exclusion_reason="non_us")
    # E — shown but has NO raw_page: not in the denominator, not sampled.
    e = _company("Echo", latest_round_amount=Decimal("500000000"))

    for co in (a, b, c, d, e):
        db.add(co)
    await db.flush()

    db.add(
        RawPage(
            company_id=a.id,
            url="https://alpha.example/",
            content="Maya Okafor — Co-founder & CEO. Previously at Stripe.",
        )
    )
    # A second page for A — the DISTINCT-company count must not double-count it.
    db.add(
        RawPage(
            company_id=a.id,
            url="https://alpha.example/team",
            content="Our team builds developer tools.",
        )
    )
    db.add(
        RawPage(
            company_id=b.id,
            url="https://bravo.example/",
            content="We build fast, reliable software for enterprises.",
        )
    )
    db.add(
        RawPage(
            company_id=c.id,
            url="https://charlie.example/",
            content="Formerly at Google. The team ships weekly.",
        )
    )
    db.add(
        RawPage(
            company_id=d.id,
            url="https://delta.example/",
            content="An ex-Amazon founder started this. CEO and CTO on staff.",
        )
    )
    await db.commit()

    summary = await run_career_history_probe(db, sample=10)

    # Denominator: A, B, C have pages and are shown. D excluded, E page-less.
    assert summary.shown_companies_with_pages == 3
    # Bio section: A (CEO / Co-founder) and C (the team). B has none.
    assert summary.companies_with_bio_section == 2
    # Tier-1: A (previously), C (formerly). B none.
    assert summary.companies_with_any_career_signal == 2
    # Tier-2 headline: A (Stripe), C (Google). D excluded despite ex-Amazon.
    assert summary.companies_with_named_prior_company == 2

    # Percentages track the counts over the 3-company denominator.
    assert summary.named_prior_pct == pytest.approx(round(2 / 3 * 100, 1))

    # Per-phrase histogram: exactly one company each for these two cues.
    assert summary.per_phrase_company_counts["previously"] == 1
    assert summary.per_phrase_company_counts["formerly"] == 1

    # Sample block: all three page-bearing shown companies fit in sample=10.
    assert summary.sample_size == 3
    # Precision-corrected rate: A + C hit a named prior; B does not → 2/3.
    assert summary.sample_named_prior_rate == pytest.approx(2 / 3, abs=1e-4)
    # Example captures surface the real employer names (order = funding order).
    assert summary.sample_example_captures == ["Stripe", "Google"]


async def test_probe_zero_sample_skips_python_scan(db: AsyncSession) -> None:
    """--sample 0 still runs the SQL aggregates but does no Python sampling."""
    co = _company("Solo", latest_round_amount=Decimal("1"))
    db.add(co)
    await db.flush()
    db.add(
        RawPage(
            company_id=co.id,
            url="https://solo.example/",
            content="ex-Netflix founder. CEO.",
        )
    )
    await db.commit()

    summary = await run_career_history_probe(db, sample=0)

    assert summary.companies_with_named_prior_company >= 1  # SQL still counts Solo
    assert summary.sample_size == 0
    assert summary.sample_named_prior_rate == 0.0
    assert summary.sample_example_captures == []
