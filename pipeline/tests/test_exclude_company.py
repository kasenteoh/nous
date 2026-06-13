"""Integration tests for the manual exclude-company lever. Requires DATABASE_URL."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline.exclude_company import run_exclude_company

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _co(slug: str) -> Company:
    return Company(
        name=slug, slug=slug, normalized_name=slug, hq_country="US"
    )


async def test_exclude_then_clear(db: AsyncSession) -> None:
    db.add(_co("acme-excl"))
    await db.commit()

    r = await run_exclude_company(db, slug="acme-excl", reason="manual", detail="junk")
    assert r.found is True
    assert r.exclusion_reason == "manual"
    co = (
        await db.execute(select(Company).where(Company.slug == "acme-excl"))
    ).scalar_one()
    assert co.exclusion_reason == "manual"
    assert co.excluded_at is not None

    r2 = await run_exclude_company(db, slug="acme-excl", clear=True)
    assert r2.exclusion_reason is None
    await db.refresh(co)
    assert co.exclusion_reason is None
    assert co.excluded_at is None


async def test_unknown_slug_reports_not_found(db: AsyncSession) -> None:
    r = await run_exclude_company(db, slug="does-not-exist-xyz")
    assert r.found is False


async def test_invalid_reason_rejected(db: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await run_exclude_company(db, slug="whatever", reason="bogus")
