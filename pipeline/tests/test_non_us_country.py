"""Tests for Task 2.3 — stop masking non-US companies as US.

Covers:
- _infer_country_from_url: pure-unit, no DB required.
- auto_create_company no longer sets hq_country='US' on insert (DB).
- enrich-companies: London-based website text → hq_country=GB + non_us (DB).
- enrich-companies: ccTLD .co.uk → hq_country=GB + non_us even when LLM
  returns no hq_country (DB).
- judge-eligibility: ccTLD inference fires there too (DB).
- US state present → hq_country=US is still inferred (DB).
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nous.db.models import Company, RawPage
from nous.db.upsert import auto_create_company
from nous.llm.prompts.company_description import CompanyDescription
from nous.llm.prompts.company_eligibility import EligibilityJudgment
from nous.pipeline.enrich_companies import _infer_country_from_url, run_enrich_companies
from nous.pipeline.judge_eligibility import run_judge_eligibility

# ---------------------------------------------------------------------------
# Pure-unit tests for _infer_country_from_url — no DATABASE_URL required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # British Isles
        ("https://example.co.uk/", "GB"),
        ("https://www.example.co.uk", "GB"),
        ("https://example.uk", "GB"),
        ("https://example.org.uk", "GB"),
        # India
        ("https://example.in", "IN"),
        ("https://example.co.in", "IN"),
        # Germany
        ("https://example.de", "DE"),
        # France
        ("https://example.fr", "FR"),
        # Canada
        ("https://example.ca", "CA"),
        # Australia
        ("https://example.com.au", "AU"),
        ("https://example.au", "AU"),
        # Brazil
        ("https://example.com.br", "BR"),
        # Netherlands
        ("https://example.nl", "NL"),
        # Singapore
        ("https://example.com.sg", "SG"),
        # Japan
        ("https://example.co.jp", "JP"),
        # Generic TLDs — no signal
        ("https://example.com", None),
        ("https://example.io", None),
        ("https://example.co", None),
        ("https://example.ai", None),
        ("https://example.tech", None),
        # Null / empty
        (None, None),
        ("", None),
        # Malformed
        ("not-a-url", None),
    ],
)
def test_infer_country_from_url(url: str | None, expected: str | None) -> None:
    assert _infer_country_from_url(url) == expected


# ---------------------------------------------------------------------------
# DB-gated integration tests
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

# Re-declare pytestmark so all tests below skip without DATABASE_URL.
pytestmark = pytestmark_db


def _london_raw_page(company_id: Any) -> RawPage:
    """Raw page whose text says 'London-based' — strong non-US signal."""
    return RawPage(
        company_id=company_id,
        url="https://fresha.com/",
        content=(
            "Fresha is the world's #1 beauty and wellness marketplace. "
            "London-based and founded in 2015, the company serves over "
            "100,000 partner venues across 120 countries. "
            "Our headquarters are at 60 Holborn Viaduct, London, EC1A 2FD. "
            "We are backed by leading investors and are growing fast across Europe."
        ) * 5,
    )


def _uk_cctld_page(company_id: Any) -> RawPage:
    """Raw page with no country text but the company website has a .co.uk domain."""
    return RawPage(
        company_id=company_id,
        url="https://noths.co.uk/",
        content=(
            "Not On The High Street is an online marketplace connecting "
            "independent sellers with customers who want something special. "
            "Our platform features thousands of unique products from small businesses. "
            "We believe in supporting creative entrepreneurs."
        ) * 5,
    )


def _us_state_page(company_id: Any) -> RawPage:
    """Raw page that names a US state — should be inferred as US."""
    return RawPage(
        company_id=company_id,
        url="https://usstartup.com/",
        content=(
            "We are a San Francisco, CA-based software company focused on "
            "developer tools. Founded in 2020, our team of 50 engineers builds "
            "cloud infrastructure for modern teams. Backed by Y Combinator."
        ) * 5,
    )


async def test_auto_create_does_not_set_us_default(db: AsyncSession) -> None:
    """auto_create_company no longer sets hq_country='US' on insert."""
    company, created = await auto_create_company(
        db,
        name="Fresha Ltd",
        website="https://fresha.com",
        discovered_via="vc_portfolio",
    )
    await db.flush()
    assert created is True
    # hq_country must be NULL — no US evidence was provided.
    assert company.hq_country is None


async def test_enrich_sets_non_us_from_llm_country(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the LLM explicitly returns hq_country='GB', the company is excluded
    with exclusion_reason='non_us' and hq_country='GB'."""
    company = Company(
        name="Fresha",
        slug="fresha-enrich-gb",
        normalized_name="fresha",
    )
    db.add(company)
    await db.flush()
    db.add(_london_raw_page(company.id))
    await db.flush()
    await db.commit()

    canned = CompanyDescription(
        description_short="A beauty and wellness marketplace based in London.",
        description_long="Long text about Fresha.",
        primary_category="marketplace",
        tags=["beauty", "wellness"],
        website_state="ok",
        is_startup=True,
        hq_country="GB",
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_enrich_companies(db)

    await db.refresh(company)
    assert company.hq_country == "GB"
    assert company.exclusion_reason == "non_us"
    assert company.excluded_at is not None
    assert company.eligibility_checked_at is not None
    assert summary.companies_excluded >= 1


async def test_enrich_sets_non_us_from_cctld_when_llm_silent(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the LLM returns no hq_country but the website is .co.uk, the
    ccTLD inference sets hq_country='GB' and triggers non_us exclusion."""
    company = Company(
        name="Not On The High Street",
        slug="noths-enrich-cctld",
        normalized_name="not on the high street",
        website="https://noths.co.uk",  # .co.uk → GB
    )
    db.add(company)
    await db.flush()
    db.add(_uk_cctld_page(company.id))
    await db.flush()
    await db.commit()

    canned = CompanyDescription(
        description_short="An online marketplace for independent sellers.",
        description_long="Long text about NOTHS.",
        primary_category="e-commerce",
        tags=["marketplace", "retail"],
        website_state="ok",
        is_startup=True,
        hq_country=None,  # LLM says nothing about country
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_enrich_companies(db)

    await db.refresh(company)
    assert company.hq_country == "GB"
    assert company.exclusion_reason == "non_us"
    assert "ccTLD" in (company.exclusion_detail or "")
    assert summary.companies_excluded >= 1


async def test_enrich_infers_us_when_state_set(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the LLM returns hq_state='CA' (a US state), the company stays
    included with hq_country='US'."""
    company = Company(
        name="US Startup Inc",
        slug="us-startup-infer",
        normalized_name="us startup inc",
        website="https://usstartup.com",
    )
    db.add(company)
    await db.flush()
    db.add(_us_state_page(company.id))
    await db.flush()
    await db.commit()

    canned = CompanyDescription(
        description_short="A San Francisco-based developer tools startup.",
        description_long="Long text about US Startup Inc.",
        primary_category="developer tools",
        tags=["saas", "cloud"],
        website_state="ok",
        is_startup=True,
        hq_city="San Francisco",
        hq_state="CA",
        hq_country=None,  # LLM doesn't explicitly state US
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    await run_enrich_companies(db)

    await db.refresh(company)
    # US state → hq_country should be inferred as 'US'
    assert company.hq_country == "US"
    assert company.exclusion_reason is None


async def test_judge_eligibility_cctld_non_us(
    committed_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """judge-eligibility also applies ccTLD inference: .co.uk → GB → non_us.

    Uses committed_session_factory because run_judge_eligibility now opens a
    fresh session per company (per-company-session resilience, PR #80), so the
    fixtures must be committed for the stage's separate sessions to see them.
    """
    async with committed_session_factory() as s1:
        company = Company(
            name="UK SaaS Ltd",
            slug="uk-saas-judge",
            normalized_name="uk saas ltd",
            website="https://uksaas.co.uk",
            description_short="A UK-based SaaS company.",
            description_long="Long text.",
        )
        s1.add(company)
        await s1.flush()
        s1.add(
            RawPage(
                company_id=company.id,
                url="https://uksaas.co.uk/",
                content="We provide HR software to businesses across the United Kingdom. "
                "Our platform is trusted by thousands of companies. " * 10,
            )
        )
        await s1.commit()
        company_id = company.id

    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=EligibilityJudgment(is_startup=True, hq_country=None)),
    )

    summary = await run_judge_eligibility(committed_session_factory)
    assert summary.companies_excluded == 1

    async with committed_session_factory() as s3:
        refetched = await s3.get(Company, company_id)
    assert refetched is not None
    assert refetched.hq_country == "GB"
    assert refetched.exclusion_reason == "non_us"
    assert "ccTLD" in (refetched.exclusion_detail or "")
