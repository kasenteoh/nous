"""DB-gated tests for the estimate-employees stage.

Source clients are monkeypatched so the stage logic — first-source-wins,
attribution, checked_at stamping, refetch back-off, and idempotency — is tested
without any network. Gated on DATABASE_URL like the other integration tests.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline.estimate_employees import run_estimate_employees

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(
    *,
    slug: str,
    name: str = "Acme",
    website: str | None = None,
    employee_count_min: int | None = None,
    employee_count_max: int | None = None,
    employee_count_source: str | None = None,
    employee_count_checked_at: datetime | None = None,
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        website=website,
        employee_count_min=employee_count_min,
        employee_count_max=employee_count_max,
        employee_count_source=employee_count_source,
        employee_count_checked_at=employee_count_checked_at,
    )


def _patch_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wellfound: tuple[int, int] | None = None,
    theorg: tuple[int, int] | None = None,
    growjo: tuple[int, int] | None = None,
    careers: tuple[int, int] | None = None,
    github: tuple[int, int] | None = None,
) -> None:
    monkeypatch.setattr(
        "nous.sources.wellfound.get_employee_range", AsyncMock(return_value=wellfound)
    )
    monkeypatch.setattr(
        "nous.sources.theorg.get_employee_range", AsyncMock(return_value=theorg)
    )
    monkeypatch.setattr(
        "nous.sources.growjo.get_employee_range", AsyncMock(return_value=growjo)
    )
    monkeypatch.setattr(
        "nous.sources.careers_jobs.count_job_listings", AsyncMock(return_value=careers)
    )
    monkeypatch.setattr(
        "nous.sources.github_org.get_employee_range", AsyncMock(return_value=github)
    )


def _client() -> MagicMock:
    # The sources are monkeypatched, so the client is never actually used.
    return MagicMock()


async def test_first_source_wins_and_records_attribution(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sources(monkeypatch, wellfound=(11, 50), theorg=(51, 200))
    company = _make_company(slug="first-wins", website="https://acme.com")
    db.add(company)
    await db.commit()

    summary = await run_estimate_employees(db, _client(), "", refetch_after_days=90)

    await db.refresh(company)
    assert (company.employee_count_min, company.employee_count_max) == (11, 50)
    assert company.employee_count_source == "wellfound"
    assert company.employee_count_checked_at is not None
    assert summary.companies_seen == 1
    assert summary.updated == 1
    assert summary.no_data == 0


async def test_falls_through_to_next_source_when_first_is_none(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sources(monkeypatch, wellfound=None, theorg=(51, 200))
    company = _make_company(slug="fall-through")
    db.add(company)
    await db.commit()

    await run_estimate_employees(db, _client(), "", refetch_after_days=90)

    await db.refresh(company)
    assert (company.employee_count_min, company.employee_count_max) == (51, 200)
    assert company.employee_count_source == "theorg"


async def test_all_sources_none_records_no_data_but_stamps_checked_at(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sources(monkeypatch)  # all None
    company = _make_company(slug="no-data", website="https://acme.com")
    db.add(company)
    await db.commit()

    summary = await run_estimate_employees(db, _client(), "tok", refetch_after_days=90)

    await db.refresh(company)
    assert company.employee_count_min is None
    assert company.employee_count_source is None
    # The attempt is still stamped so we don't re-probe every run.
    assert company.employee_count_checked_at is not None
    assert summary.no_data == 1
    assert summary.updated == 0


async def test_recently_checked_company_is_skipped(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sources(monkeypatch, wellfound=(11, 50))
    company = _make_company(
        slug="recent",
        employee_count_min=11,
        employee_count_max=50,
        employee_count_source="wellfound",
        employee_count_checked_at=datetime.now(tz=UTC),
    )
    db.add(company)
    await db.commit()

    summary = await run_estimate_employees(db, _client(), "", refetch_after_days=90)

    # Has a count AND was just checked -> excluded by the eligibility query.
    assert summary.companies_seen == 0


async def test_stale_company_is_reprocessed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sources(monkeypatch, wellfound=(201, 500))
    stale = datetime.now(tz=UTC) - timedelta(days=91)
    company = _make_company(
        slug="stale",
        employee_count_min=11,
        employee_count_max=50,
        employee_count_source="wellfound",
        employee_count_checked_at=stale,
    )
    db.add(company)
    await db.commit()

    summary = await run_estimate_employees(db, _client(), "", refetch_after_days=90)

    await db.refresh(company)
    assert summary.companies_seen == 1
    assert summary.updated == 1
    assert (company.employee_count_min, company.employee_count_max) == (201, 500)
    assert company.employee_count_checked_at is not None
    assert company.employee_count_checked_at > stale


async def test_unchanged_when_same_values_returned(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sources(monkeypatch, wellfound=(11, 50))
    stale = datetime.now(tz=UTC) - timedelta(days=91)
    company = _make_company(
        slug="unchanged",
        employee_count_min=11,
        employee_count_max=50,
        employee_count_source="wellfound",
        employee_count_checked_at=stale,
    )
    db.add(company)
    await db.commit()

    summary = await run_estimate_employees(db, _client(), "", refetch_after_days=90)

    assert summary.companies_seen == 1
    assert summary.unchanged == 1
    assert summary.updated == 0
