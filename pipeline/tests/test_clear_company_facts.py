"""Tests for clear-company-facts — the standalone total/status clearer.

The wave/terrafirma gap: delete-round's clear flags need a round to ride on;
this lever clears the company-level facts directly. Pins: dry-run previews
without writing, apply clears fields + their ✓ rows and NOTHING else,
no-op semantics, error paths. Requires DATABASE_URL.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification
from nous.pipeline.clear_company_facts import (
    ClearCompanyFactsError,
    run_clear_company_facts,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _co(slug: str, **kw: object) -> Company:
    return Company(
        name=slug.replace("-", " ").title(),
        slug=slug,
        normalized_name=slug.replace("-", " "),
        description_short="A shown company.",
        **kw,  # type: ignore[arg-type]
    )


async def _seed(db: AsyncSession) -> Company:
    co = _co(
        "wave-facts-test",
        status="shut_down",
        status_source_url="https://gn.example.com/primary-wave-shutdown",
        total_raised_usd=Decimal("115000000"),
        total_raised_source_url="https://gn.example.com/terrafirma-inc-115m",
    )
    db.add(co)
    await db.flush()
    for kind, src in (
        ("total_raised", "https://gn.example.com/terrafirma-inc-115m"),
        ("status", "https://gn.example.com/primary-wave-shutdown"),
    ):
        db.add(
            FactVerification(
                company_id=co.id,
                fact_kind=kind,
                fact_ref="",
                source_url=src,
                claim=f"claim about {kind}",
                verdict="supported",
                supporting_quote="quote",
                prompt_version="2026-07-17.1",
            )
        )
    # An unrelated funding_round ✓ that must SURVIVE every clear.
    db.add(
        FactVerification(
            company_id=co.id,
            fact_kind="funding_round",
            fact_ref="some-round-id",
            source_url="https://real.example.com/round",
            claim="a real round",
            verdict="supported",
            supporting_quote="quote",
            prompt_version="2026-07-17.1",
        )
    )
    await db.commit()
    return co


async def test_dry_run_previews_and_writes_nothing(db: AsyncSession) -> None:
    co = await _seed(db)
    summary = await run_clear_company_facts(
        db, slug=co.slug, clear_total=True, clear_status=True
    )
    assert summary.dry_run is True
    assert summary.total_raised_cleared and summary.status_reset
    assert summary.total_raised_was == "$115,000,000"
    assert summary.status_was == "shut_down"
    assert summary.verifications_deleted == 2

    await db.refresh(co)
    assert co.total_raised_usd == Decimal("115000000")
    assert co.status == "shut_down"
    verifs = (
        (
            await db.execute(
                select(FactVerification).where(FactVerification.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(verifs) == 3


async def test_apply_clears_fields_and_their_verifications_only(
    db: AsyncSession,
) -> None:
    co = await _seed(db)
    summary = await run_clear_company_facts(
        db, slug=co.slug, clear_total=True, clear_status=True, dry_run=False
    )
    assert summary.verifications_deleted == 2
    await db.refresh(co)
    assert co.total_raised_usd is None
    assert co.total_raised_source_url is None
    assert co.total_raised_as_of is None
    assert co.status == "active"
    assert co.status_source_url is None
    verifs = (
        (
            await db.execute(
                select(FactVerification).where(FactVerification.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert [v.fact_kind for v in verifs] == ["funding_round"]  # survivor

    # Idempotent: nothing left to clear; summary flags stay false.
    again = await run_clear_company_facts(
        db, slug=co.slug, clear_total=True, clear_status=True, dry_run=False
    )
    assert again.total_raised_cleared is False
    assert again.status_reset is False
    assert again.verifications_deleted == 0


async def test_single_flag_clears_only_that_fact(db: AsyncSession) -> None:
    co = await _seed(db)
    summary = await run_clear_company_facts(
        db, slug=co.slug, clear_status=True, dry_run=False
    )
    assert summary.status_reset is True
    assert summary.total_raised_cleared is False
    assert summary.verifications_deleted == 1
    await db.refresh(co)
    assert co.status == "active"
    assert co.total_raised_usd == Decimal("115000000")  # untouched


async def test_error_paths(db: AsyncSession) -> None:
    co = await _seed(db)
    with pytest.raises(ClearCompanyFactsError, match="nothing to do"):
        await run_clear_company_facts(db, slug=co.slug)
    with pytest.raises(ClearCompanyFactsError, match="no company"):
        await run_clear_company_facts(
            db, slug="does-not-exist", clear_total=True
        )
