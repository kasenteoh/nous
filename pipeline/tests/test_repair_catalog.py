"""Integration tests for the one-time repair-catalog stage.

Covers: suffix rename, collision-merge, LSIP husk delete, LSIP-with-data
exclude, parked-description reset, SellRaze-style false-positive safety,
and run-twice idempotency. Requires DATABASE_URL.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, RawPage
from nous.pipeline.repair_catalog import run_repair_catalog

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _co(name: str, slug: str, **kw: object) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        **kw,
    )


async def test_both_funds_suffix_renamed(db: AsyncSession) -> None:
    co = _co("1047 gamesLSVP and LSIP Investment", "1047-gameslsvp-and-lsip-investment")
    db.add(co)
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.names_cleaned == 1

    await db.refresh(co)
    assert co.name == "1047 games"
    # normalize_name strips internal whitespace (its documented dedup-key
    # behavior: "Open AI" and "OpenAI" must collide), so the canonical form is
    # "1047games", not "1047 games".
    assert co.normalized_name == "1047games"
    assert co.slug == "1047-games"
    assert co.exclusion_reason is None


async def test_both_funds_collision_merges_into_existing(db: AsyncSession) -> None:
    clean = _co("Composio", "composio", description_short="Tool-use platform.")
    suffixed = _co("ComposioLSVP and LSIP Investment", "composiolsvp-and-lsip-investment")
    db.add_all([clean, suffixed])
    await db.commit()
    suffixed_id = suffixed.id

    summary = await run_repair_catalog(db)
    assert summary.merged == 1

    gone = (
        await db.execute(select(Company).where(Company.id == suffixed_id))
    ).scalar_one_or_none()
    assert gone is None
    survivor = (
        await db.execute(select(Company).where(Company.slug == "composio"))
    ).scalar_one()
    assert survivor.description_short == "Tool-use platform."


async def test_lsip_husk_deleted_but_linked_row_excluded(db: AsyncSession) -> None:
    husk = _co("ApnaLSIP Investment", "apnalsip-investment")
    funded = _co("AckoLSIP Investment", "ackolsip-investment")
    db.add_all([husk, funded])
    await db.flush()
    db.add(
        FundingRound(
            company_id=funded.id, round_type="Series D", announced_date=date(2024, 1, 1)
        )
    )
    await db.commit()
    husk_id = husk.id

    summary = await run_repair_catalog(db)
    assert summary.lsip_deleted == 1
    assert summary.lsip_excluded == 1

    assert (
        await db.execute(select(Company).where(Company.id == husk_id))
    ).scalar_one_or_none() is None
    await db.refresh(funded)
    assert funded.exclusion_reason == "non_us"
    assert funded.name == "Acko"  # name still cleaned on the kept row


async def test_parked_description_reset(db: AsyncSession) -> None:
    parked = _co(
        "Ninegag",
        "ninegag-repair",
        website="https://ninegag.ai",
        description_short=(
            "The domain ninegag.ai is listed for sale on Spaceship.com; no "
            "product or company information is available."
        ),
        description_long="Parked.",
    )
    # Real company whose copy mentions selling — must NOT be touched.
    sellraze = _co(
        "SellRaze",
        "sellraze-repair",
        website="https://sellraze.com",
        description_short=(
            "SellRaze lets sellers list items for sale across marketplaces "
            "using image recognition."
        ),
    )
    db.add_all([parked, sellraze])
    await db.flush()
    db.add(RawPage(company_id=parked.id, url="https://ninegag.ai/", content="x" * 300))
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.parked_reset == 1

    await db.refresh(parked)
    assert parked.website is None
    assert parked.website_resolved_at is None
    assert parked.description_short is None
    assert parked.description_long is None
    assert parked.rejected_urls == ["https://ninegag.ai"]
    pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == parked.id))
    ).scalars().all()
    assert pages == []

    await db.refresh(sellraze)
    assert sellraze.description_short is not None
    assert sellraze.website == "https://sellraze.com"


async def test_for_sale_lander_reset_by_page_content(db: AsyncSession) -> None:
    # Foodology shape: the LLM narrated a for-sale lander as a real "culinary
    # content platform". Its prose never says "domain", so Pass 2's description
    # patterns miss it — but the scraped page literally says "<host> is for sale".
    # Pass 3 re-judges the page content (ground truth) and resets the row.
    lander = _co(
        "Foodology",
        "foodology-repair",
        website="https://foodology.com",
        website_resolved_at=datetime(2026, 6, 11, tzinfo=UTC),
        description_short=(
            "Foodology is a culinary content platform exploring global "
            "traditions, based on a site that is currently for sale."
        ),
        description_long="A culinary content platform.",
        primary_category="content",
        last_enriched_at=datetime(2026, 6, 12, tzinfo=UTC),
    )
    # Real company whose homepage copy mentions selling — must NOT be touched.
    real = _co(
        "SellRaze",
        "sellraze-repair3",
        website="https://sellraze.com",
        description_short="SellRaze lists your items for sale across marketplaces.",
    )
    db.add_all([lander, real])
    await db.flush()
    db.add(
        RawPage(
            company_id=lander.id,
            url="https://foodology.com/",
            content=(
                "foodology.com is for sale.\n\nExploring Culinary Delights with "
                "Foodology\n\nDiscovering Global Culinary Traditions."
            ),
        )
    )
    db.add(
        RawPage(
            company_id=real.id,
            url="https://sellraze.com/",
            content=(
                "SellRaze | The fastest way to sell your stuff\n"
                "List items for sale across every marketplace."
            ),
        )
    )
    await db.commit()

    summary = await run_repair_catalog(db)

    await db.refresh(lander)
    assert lander.website is None
    assert lander.website_resolved_at is None
    assert lander.description_short is None
    assert lander.description_long is None
    assert lander.primary_category is None
    assert lander.last_enriched_at is None
    assert lander.rejected_urls == ["https://foodology.com"]
    assert (
        (await db.execute(select(RawPage).where(RawPage.company_id == lander.id)))
        .scalars()
        .all()
        == []
    )
    assert summary.for_sale_reset == 1

    # The real company is left entirely alone.
    await db.refresh(real)
    assert real.website == "https://sellraze.com"
    assert real.description_short is not None
    assert (
        (await db.execute(select(RawPage).where(RawPage.company_id == real.id)))
        .scalars()
        .first()
        is not None
    )

    # Idempotent: the reset cleared the website + dropped the page, so a second
    # run re-selects nothing.
    second = await run_repair_catalog(db)
    assert second.for_sale_reset == 0


async def test_repair_is_idempotent(db: AsyncSession) -> None:
    db.add(_co("FoxyLSIP Investment", "foxylsip-investment"))
    await db.commit()

    first = await run_repair_catalog(db)
    assert first.lsip_deleted == 1
    second = await run_repair_catalog(db)
    assert (
        second.names_cleaned
        == second.lsip_deleted
        == second.lsip_excluded
        == second.merged
        == second.parked_reset
        == 0
    )


async def test_dry_run_writes_nothing(db: AsyncSession) -> None:
    co = _co("AstroLSVP and LSIP Investment", "astrolsvp-and-lsip-investment")
    db.add(co)
    await db.commit()

    summary = await run_repair_catalog(db, dry_run=True)
    assert summary.names_cleaned == 1  # counted as would-do

    await db.refresh(co)
    assert co.name == "AstroLSVP and LSIP Investment"  # unchanged
