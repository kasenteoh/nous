"""Integration tests for the one-time repair-catalog stage.

Covers: suffix rename, collision-merge, LSIP husk delete, LSIP-with-data
exclude, parked-description reset, SellRaze-style false-positive safety,
placeholder-name rename/exclude, and run-twice idempotency. Requires DATABASE_URL.
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle, RawPage
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


# ---------------------------------------------------------------------------
# Pass 3: placeholder name repair
# ---------------------------------------------------------------------------


async def test_placeholder_bracketed_name_renamed_from_domain(db: AsyncSession) -> None:
    """A row with name "[untitled]" and a website is renamed from the domain apex."""
    # Use a unique slug so it never collides with other test rows.
    co = _co(
        "[untitled]",
        "rep3-untitled-placeholder",
        website="https://untitled.stream/",
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.placeholder_renamed == 1
    assert summary.placeholder_excluded == 0

    await db.refresh(co)
    # Domain apex of "untitled.stream" is "untitled" → title-case → "Untitled".
    assert co.name == "Untitled"
    assert co.normalized_name == "untitled"
    assert co.slug == "untitled"
    assert co.exclusion_reason is None


async def test_placeholder_no_website_soft_excluded(db: AsyncSession) -> None:
    """A row with name "[TBD]" and no website is soft-excluded as 'manual'."""
    co = _co(
        "[TBD]",
        "rep3-tbd-placeholder",
        website=None,
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.placeholder_excluded == 1
    assert summary.placeholder_renamed == 0

    await db.refresh(co)
    assert co.exclusion_reason == "manual"
    assert co.exclusion_detail is not None
    # Name is left as-is — no rename possible without a domain to derive from.
    assert co.name == "[TBD]"


async def test_placeholder_repair_is_idempotent(db: AsyncSession) -> None:
    """Running repair-catalog twice on a placeholder row changes nothing on the second run."""
    co = _co(
        "[untitled]",
        "rep3-untitled-idempotent",
        website="https://untitled2.stream/",
    )
    db.add(co)
    await db.commit()

    first = await run_repair_catalog(db)
    assert first.placeholder_renamed == 1

    second = await run_repair_catalog(db)
    # After the first pass the name is "Untitled2" — no longer a placeholder.
    assert second.placeholder_renamed == 0
    assert second.placeholder_excluded == 0


async def test_placeholder_dry_run_counts_but_writes_nothing(db: AsyncSession) -> None:
    """--dry-run counts placeholder rows but makes no DB changes."""
    co = _co(
        "[stealth]",
        "rep3-stealth-placeholder",
        website="https://example.com/",
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_catalog(db, dry_run=True)
    assert summary.placeholder_renamed == 1
    assert summary.dry_run is True

    await db.refresh(co)
    assert co.name == "[stealth]"  # untouched in dry-run


# ── Pass 4: news article → funding round links ───────────────────────────────


def _article(company_id: object, url: str) -> NewsArticle:
    return NewsArticle(
        company_id=company_id,
        url=url,
        title="Round announced",
        source="techcrunch.com",
        raw_content="body",
        processed=True,
    )


async def test_primary_news_article_linked_to_round(db: AsyncSession) -> None:
    """An already-processed article whose url IS a round's primary_news_url gets
    funding_round_id backfilled; an unrelated article stays unlinked."""
    co = _co("Linko", "rep4-linko")
    db.add(co)
    await db.flush()
    tc = "https://techcrunch.com/linko-series-a"
    fr = FundingRound(company_id=co.id, primary_news_url=tc, round_type="Series A")
    primary = _article(co.id, tc)
    other = _article(co.id, "https://techcrunch.com/linko-profile")
    db.add_all([fr, primary, other])
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.news_round_links_set == 1

    await db.refresh(primary)
    await db.refresh(other)
    assert primary.funding_round_id == fr.id
    assert other.funding_round_id is None

    # Idempotent: a second run selects nothing (IS NULL guard).
    second = await run_repair_catalog(db)
    assert second.news_round_links_set == 0


async def test_news_round_link_dry_run_counts_only(db: AsyncSession) -> None:
    co = _co("Linkodry", "rep4-linkodry")
    db.add(co)
    await db.flush()
    tc = "https://techcrunch.com/linkodry-seed"
    fr = FundingRound(company_id=co.id, primary_news_url=tc)
    art = _article(co.id, tc)
    db.add_all([fr, art])
    await db.commit()

    summary = await run_repair_catalog(db, dry_run=True)
    assert summary.news_round_links_set == 1

    await db.refresh(art)
    assert art.funding_round_id is None  # untouched in dry-run


async def test_news_round_link_requires_same_company(db: AsyncSession) -> None:
    """A URL collision across companies must not cross-link: the join requires
    company match, so another company's article stays unlinked."""
    a = _co("LinkoA", "rep4-linko-a")
    b = _co("LinkoB", "rep4-linko-b")
    db.add_all([a, b])
    await db.flush()
    tc = "https://techcrunch.com/linko-ab-round"
    fr = FundingRound(company_id=a.id, primary_news_url=tc)
    art_b = _article(b.id, tc)  # same URL, DIFFERENT company
    db.add_all([fr, art_b])
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.news_round_links_set == 0
    await db.refresh(art_b)
    assert art_b.funding_round_id is None


# ── Pass 5: duplicate Google-News headline rows (2026-07-16 QA, blue-origin) ─


async def test_gn_duplicate_titles_collapsed(db: AsyncSession) -> None:
    """Three GN rows with one title for one company keep the round-linked
    survivor; a publisher-URL row and another company's identical title are
    untouched. Idempotent."""
    co = _co("BO Dedup Co", "bo-dedup-co")
    other = _co("Other Co", "other-co-gn")
    db.add_all([co, other])
    await db.flush()

    rnd = FundingRound(
        company_id=co.id, round_type="Series A", amount_raised=10_000_000_000
    )
    db.add(rnd)
    await db.flush()

    title = "Bezos seeks $10B - MSN"
    keeper = NewsArticle(
        company_id=co.id,
        url="https://news.google.com/rss/articles/CBMiKEEP?oc=5",
        title=title,
        source="news.google.com",
        raw_content="b",
        funding_round_id=rnd.id,
    )
    dup1 = NewsArticle(
        company_id=co.id,
        url="https://news.google.com/rss/articles/CBMiDUP1?oc=5",
        title=title,
        source="news.google.com",
        raw_content="b",
    )
    dup2 = NewsArticle(
        company_id=co.id,
        url="https://news.google.com/rss/articles/CBMiDUP2?oc=5",
        title=title.upper(),  # case-insensitive grouping
        source="news.google.com",
        raw_content="b",
    )
    publisher_row = NewsArticle(
        company_id=co.id,
        url="https://reuters.com/blue-origin-10b",
        title=title,
        source="reuters.com",
        raw_content="b",
    )
    other_co_row = NewsArticle(
        company_id=other.id,
        url="https://news.google.com/rss/articles/CBMiOTHERCO?oc=5",
        title=title,
        source="news.google.com",
        raw_content="b",
    )
    db.add_all([keeper, dup1, dup2, publisher_row, other_co_row])
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.gn_duplicate_articles_deleted == 2

    remaining = (
        (
            await db.execute(
                select(NewsArticle.url).order_by(NewsArticle.url)
            )
        )
        .scalars()
        .all()
    )
    assert "https://news.google.com/rss/articles/CBMiKEEP?oc=5" in remaining
    assert "https://reuters.com/blue-origin-10b" in remaining
    assert "https://news.google.com/rss/articles/CBMiOTHERCO?oc=5" in remaining
    assert len(remaining) == 3

    second = await run_repair_catalog(db)
    assert second.gn_duplicate_articles_deleted == 0


async def test_gn_duplicate_dry_run_counts_only(db: AsyncSession) -> None:
    co = _co("Dry GN Co", "dry-gn-co")
    db.add(co)
    await db.flush()
    for token in ("A", "B"):
        db.add(
            NewsArticle(
                company_id=co.id,
                url=f"https://news.google.com/rss/articles/CBMi{token}?oc=5",
                title="One headline - MSN",
                source="news.google.com",
                raw_content="b",
            )
        )
    await db.commit()

    summary = await run_repair_catalog(db, dry_run=True)
    assert summary.gn_duplicate_articles_deleted == 1
    count = (
        await db.execute(select(func.count()).select_from(NewsArticle))
    ).scalar_one()
    assert count == 2


async def test_gn_duplicate_linked_to_different_round_is_spared(
    db: AsyncSession,
) -> None:
    """A GN duplicate linked to a DIFFERENT round than the survivor is never
    deleted — killing it would strand that round's only exact coverage link."""
    co = _co("Two Links Co", "two-links-co")
    db.add(co)
    await db.flush()
    round_a = FundingRound(
        company_id=co.id, round_type="Series A", amount_raised=10_000_000
    )
    round_b = FundingRound(
        company_id=co.id, round_type="Series B", amount_raised=50_000_000
    )
    db.add_all([round_a, round_b])
    await db.flush()

    title = "Two Links Co raises funding - MSN"
    db.add_all(
        [
            NewsArticle(
                company_id=co.id,
                url="https://news.google.com/rss/articles/CBMiLINKA?oc=5",
                title=title,
                source="news.google.com",
                raw_content="b",
                funding_round_id=round_a.id,
            ),
            NewsArticle(
                company_id=co.id,
                url="https://news.google.com/rss/articles/CBMiLINKB?oc=5",
                title=title,
                source="news.google.com",
                raw_content="b",
                funding_round_id=round_b.id,
            ),
        ]
    )
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.gn_duplicate_articles_deleted == 0
    count = (
        await db.execute(select(func.count()).select_from(NewsArticle))
    ).scalar_one()
    assert count == 2
