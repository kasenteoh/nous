"""Tests for delete-round — the surgical wrong-entity round scalpel.

The lever must delete exactly the selected round plus its wrong-entity side
effects (linked/primary articles, a stated total and non-active status sourced
from the same purged URLs, the round's ✓ verifications) and NOTHING else —
sibling rounds, unrelated articles, and unrelated verifications survive.
Ambiguity always fails loudly; a dry-run never writes. Requires DATABASE_URL.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification, FundingRound, NewsArticle
from nous.pipeline.delete_round import DeleteRoundError, run_delete_round

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_WRONG_URL = "https://techcrunch.example.com/im8-takes-1b"
_GOOD_URL = "https://businesswire.example.com/bespoke-40m-series-a"


def _co(slug: str, **kw: object) -> Company:
    return Company(
        name=slug.replace("-", " ").title(),
        slug=slug,
        normalized_name=slug.replace("-", " "),
        description_short="A shown company.",
        **kw,  # type: ignore[arg-type]
    )


async def _seed_bespoke(db: AsyncSession) -> tuple[Company, FundingRound, FundingRound]:
    """The bespoke-labs shape: one real round, one wrong-entity round whose
    primary article also sourced the stated total + a ✓ verification."""
    co = _co(
        "bespoke-labs-test",
        total_raised_usd=Decimal("1000000000"),
        total_raised_source_url=_WRONG_URL,
    )
    db.add(co)
    await db.flush()
    good = FundingRound(
        company_id=co.id,
        round_type="Series A",
        amount_raised=Decimal("40000000"),
        primary_news_url=_GOOD_URL,
    )
    wrong = FundingRound(
        company_id=co.id,
        amount_raised=Decimal("1000000000"),
        primary_news_url=_WRONG_URL,
    )
    db.add_all([good, wrong])
    await db.flush()
    db.add_all(
        [
            NewsArticle(
                company_id=co.id,
                url=_WRONG_URL,
                title="David Beckham's health drink startup IM8 takes $1B",
                source="techcrunch.example.com",
                raw_content="IM8 raised $1B from General Catalyst.",
                funding_round_id=wrong.id,
            ),
            NewsArticle(
                company_id=co.id,
                url="https://news.google.example.com/im8-syndicated",
                title="IM8 takes $1B - syndicated",
                source="news.google.example.com",
                raw_content="IM8 raised $1B.",
                funding_round_id=wrong.id,
            ),
            NewsArticle(
                company_id=co.id,
                url=_GOOD_URL,
                title="Bespoke Labs raises $40M Series A",
                source="businesswire.example.com",
                raw_content="Bespoke Labs raised $40M.",
                funding_round_id=good.id,
            ),
        ]
    )
    db.add_all(
        [
            FactVerification(
                company_id=co.id,
                fact_kind="funding_round",
                fact_ref=str(wrong.id),
                source_url=_WRONG_URL,
                claim="Bespoke Labs raised $1.0B.",
                verdict="supported",
                supporting_quote="takes $1B",
                prompt_version="2026-07-17.1",
            ),
            FactVerification(
                company_id=co.id,
                fact_kind="total_raised",
                fact_ref="",
                source_url=_WRONG_URL,
                claim="Bespoke Labs has raised a total of $1.0B.",
                verdict="supported",
                supporting_quote="takes $1B",
                prompt_version="2026-07-17.1",
            ),
            FactVerification(
                company_id=co.id,
                fact_kind="funding_round",
                fact_ref=str(good.id),
                source_url=_GOOD_URL,
                claim="Bespoke Labs raised $40.0M in its Series A round.",
                verdict="supported",
                supporting_quote="raises $40M Series A",
                prompt_version="2026-07-17.1",
            ),
        ]
    )
    await db.commit()
    return co, good, wrong


async def test_apply_deletes_round_articles_total_and_verifications(
    db: AsyncSession,
) -> None:
    co, good, wrong = await _seed_bespoke(db)

    summary = await run_delete_round(
        db, slug=co.slug, amount=Decimal("1000000000"), dry_run=False
    )
    assert summary.articles_deleted == 2
    assert summary.total_raised_cleared is True
    assert summary.verifications_deleted == 2  # the round's + the total's

    rounds = (
        (await db.execute(select(FundingRound).where(FundingRound.company_id == co.id)))
        .scalars()
        .all()
    )
    assert [r.amount_raised for r in rounds] == [Decimal("40000000")]

    articles = (
        (await db.execute(select(NewsArticle).where(NewsArticle.company_id == co.id)))
        .scalars()
        .all()
    )
    assert [a.url for a in articles] == [_GOOD_URL]

    verifs = (
        (
            await db.execute(
                select(FactVerification).where(FactVerification.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert [v.fact_ref for v in verifs] == [str(good.id)]

    await db.refresh(co)
    assert co.total_raised_usd is None
    assert co.total_raised_source_url is None
    assert co.total_raised_as_of is None
    assert co.funding_round_count == 1

    # Idempotent: the post-condition already holds; a re-run selects nothing.
    with pytest.raises(DeleteRoundError, match="no round"):
        await run_delete_round(
            db, slug=co.slug, amount=Decimal("1000000000"), dry_run=False
        )


async def test_dry_run_reports_but_writes_nothing(db: AsyncSession) -> None:
    co, _, _ = await _seed_bespoke(db)
    summary = await run_delete_round(db, slug=co.slug, amount=Decimal("1000000000"))
    assert summary.dry_run is True
    assert summary.articles_deleted == 2
    assert summary.total_raised_cleared is True

    rounds = (
        (await db.execute(select(FundingRound).where(FundingRound.company_id == co.id)))
        .scalars()
        .all()
    )
    assert len(rounds) == 2
    await db.refresh(co)
    assert co.total_raised_usd == Decimal("1000000000")


async def test_ambiguous_amount_fails_listing_ids_and_round_id_resolves(
    db: AsyncSession,
) -> None:
    co = _co("ambiguous-co")
    db.add(co)
    await db.flush()
    r1 = FundingRound(
        company_id=co.id, round_type="Series E", amount_raised=Decimal("66000000")
    )
    r2 = FundingRound(
        company_id=co.id,
        round_type="Series E extension",
        amount_raised=Decimal("66000000"),
    )
    db.add_all([r1, r2])
    await db.commit()

    with pytest.raises(DeleteRoundError, match="2 rounds match"):
        await run_delete_round(db, slug=co.slug, amount=Decimal("66000000"))

    summary = await run_delete_round(
        db, slug=co.slug, round_id=r2.id, dry_run=False
    )
    assert summary.round_id == str(r2.id)
    rounds = (
        (await db.execute(select(FundingRound).where(FundingRound.company_id == co.id)))
        .scalars()
        .all()
    )
    assert [r.round_type for r in rounds] == ["Series E"]


async def test_status_reset_when_sourced_from_purged_article(db: AsyncSession) -> None:
    url = "https://gn.example.com/wave-shut-down"
    co = _co("wave-test", status="shut_down", status_source_url=url)
    db.add(co)
    await db.flush()
    wrong = FundingRound(
        company_id=co.id, amount_raised=Decimal("2200000000"), primary_news_url=url
    )
    db.add(wrong)
    await db.commit()

    summary = await run_delete_round(
        db, slug=co.slug, amount=Decimal("2200000000"), dry_run=False
    )
    assert summary.status_reset is True
    await db.refresh(co)
    assert co.status == "active"
    assert co.status_source_url is None


async def test_clear_total_flag_covers_out_of_purge_set_source(
    db: AsyncSession,
) -> None:
    """The bespoke-labs residual: the stated total's source is a DIFFERENT
    syndication URL of the wrong-entity story, so the automatic URL match
    misses it. --clear-total forces the clear (and kills the total's ✓);
    without the flag the total must survive untouched."""
    other_syndication = "https://gn.example.com/im8-1b-other-syndication"
    co = _co(
        "clear-total-co",
        total_raised_usd=Decimal("1000000000"),
        total_raised_source_url=other_syndication,
    )
    db.add(co)
    await db.flush()
    wrong = FundingRound(
        company_id=co.id,
        amount_raised=Decimal("1000000000"),
        primary_news_url=_WRONG_URL,
    )
    db.add(wrong)
    await db.flush()
    db.add(
        FactVerification(
            company_id=co.id,
            fact_kind="total_raised",
            fact_ref="",
            source_url=other_syndication,
            claim="Clear Total Co has raised a total of $1.0B.",
            verdict="supported",
            supporting_quote="takes $1B",
            prompt_version="2026-07-17.1",
        )
    )
    await db.commit()

    # Without the flag: the out-of-set source dodges the match (the prod
    # dry-run that motivated the flag showed exactly this).
    preview = await run_delete_round(db, slug=co.slug, amount=Decimal("1000000000"))
    assert preview.total_raised_cleared is False

    summary = await run_delete_round(
        db,
        slug=co.slug,
        amount=Decimal("1000000000"),
        clear_total=True,
        dry_run=False,
    )
    assert summary.total_raised_cleared is True
    assert summary.verifications_deleted == 1  # the total's ✓
    await db.refresh(co)
    assert co.total_raised_usd is None
    assert co.total_raised_source_url is None
    verifs = (
        (
            await db.execute(
                select(FactVerification).where(FactVerification.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert verifs == []


async def test_clear_status_flag_covers_out_of_purge_set_source(
    db: AsyncSession,
) -> None:
    """The wave residual: "shut down" sourced from a URL outside the purge
    set. --clear-status resets it (and kills the status ✓). Without the flag
    it survives. The flag is a no-op on an active status."""
    outside = "https://gn.example.com/primary-wave-shut-down"
    co = _co("clear-status-co", status="shut_down", status_source_url=outside)
    db.add(co)
    await db.flush()
    wrong = FundingRound(
        company_id=co.id,
        amount_raised=Decimal("2200000000"),
        primary_news_url=_WRONG_URL,
    )
    db.add(wrong)
    await db.flush()
    db.add(
        FactVerification(
            company_id=co.id,
            fact_kind="status",
            fact_ref="",
            source_url=outside,
            claim="Clear Status Co has shut down.",
            verdict="supported",
            supporting_quote="shut down",
            prompt_version="2026-07-17.1",
        )
    )
    await db.commit()

    preview = await run_delete_round(db, slug=co.slug, amount=Decimal("2200000000"))
    assert preview.status_reset is False

    summary = await run_delete_round(
        db,
        slug=co.slug,
        amount=Decimal("2200000000"),
        clear_status=True,
        dry_run=False,
    )
    assert summary.status_reset is True
    assert summary.verifications_deleted == 1  # the status ✓
    await db.refresh(co)
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
    assert verifs == []

    # No-op when already active: re-seed a round, flag set, nothing to reset.
    r2 = FundingRound(company_id=co.id, amount_raised=Decimal("5000000"))
    db.add(r2)
    await db.commit()
    summary2 = await run_delete_round(
        db, slug=co.slug, amount=Decimal("5000000"), clear_status=True, dry_run=False
    )
    assert summary2.status_reset is False


async def test_both_clear_flags_together_and_preview_shows_doomed_values(
    db: AsyncSession,
) -> None:
    """Both flags on one dispatch: total + status cleared, both ✓ kinds die,
    and the dry-run preview names the values being destroyed (the operator
    must see WHAT a forced clear kills, not just that it will)."""
    total_src = "https://gn.example.com/other-syndication"
    status_src = "https://gn.example.com/shutdown-story"
    co = _co(
        "both-flags-co",
        total_raised_usd=Decimal("1200000000"),
        total_raised_source_url=total_src,
        status="shut_down",
        status_source_url=status_src,
    )
    db.add(co)
    await db.flush()
    wrong = FundingRound(
        company_id=co.id,
        amount_raised=Decimal("66000000"),
        primary_news_url=_WRONG_URL,
    )
    db.add(wrong)
    await db.flush()
    for kind, src in (("total_raised", total_src), ("status", status_src)):
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
    await db.commit()

    preview = await run_delete_round(
        db,
        slug=co.slug,
        amount=Decimal("66000000"),
        clear_total=True,
        clear_status=True,
    )
    assert preview.total_raised_cleared and preview.status_reset
    assert preview.total_raised_was == "$1,200,000,000"
    assert preview.total_raised_source_was == total_src
    assert preview.status_was == "shut_down"
    assert preview.status_source_was == status_src
    assert preview.verifications_deleted == 2

    summary = await run_delete_round(
        db,
        slug=co.slug,
        amount=Decimal("66000000"),
        clear_total=True,
        clear_status=True,
        dry_run=False,
    )
    assert summary.verifications_deleted == 2
    await db.refresh(co)
    assert co.total_raised_usd is None
    assert co.status == "active"
    verifs = (
        (
            await db.execute(
                select(FactVerification).where(FactVerification.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert verifs == []


async def test_status_verification_deleted_on_url_matched_reset(
    db: AsyncSession,
) -> None:
    """The URL-matched status reset (no flag) must also delete the status ✓ —
    previously only total_raised verifications were purged."""
    url = "https://gn.example.com/wave-shut-down"
    co = _co("status-verif-co", status="shut_down", status_source_url=url)
    db.add(co)
    await db.flush()
    wrong = FundingRound(
        company_id=co.id, amount_raised=Decimal("2200000000"), primary_news_url=url
    )
    db.add(wrong)
    await db.flush()
    db.add(
        FactVerification(
            company_id=co.id,
            fact_kind="status",
            fact_ref="",
            source_url=url,
            claim="Status Verif Co has shut down.",
            verdict="supported",
            supporting_quote="shut down",
            prompt_version="2026-07-17.1",
        )
    )
    await db.commit()

    summary = await run_delete_round(
        db, slug=co.slug, amount=Decimal("2200000000"), dry_run=False
    )
    assert summary.status_reset is True
    assert summary.verifications_deleted == 1
    verifs = (
        (
            await db.execute(
                select(FactVerification).where(FactVerification.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert verifs == []


async def test_keep_articles_and_error_paths(db: AsyncSession) -> None:
    co, _, wrong = await _seed_bespoke(db)

    summary = await run_delete_round(
        db,
        slug=co.slug,
        amount=Decimal("1000000000"),
        purge_articles=False,
        dry_run=False,
    )
    assert summary.articles_deleted == 0
    # Articles survive; their round link SET-NULLs via the 0044 FK.
    articles = (
        (await db.execute(select(NewsArticle).where(NewsArticle.company_id == co.id)))
        .scalars()
        .all()
    )
    assert len(articles) == 3
    assert all(
        a.funding_round_id is None for a in articles if a.url != _GOOD_URL
    )
    # No purged articles → primary URL is still the purge set for total/status
    # clearing (the round's own source).
    await db.refresh(co)
    assert co.total_raised_usd is None

    with pytest.raises(DeleteRoundError, match="no company"):
        await run_delete_round(db, slug="does-not-exist", amount=Decimal("1"))
    with pytest.raises(DeleteRoundError, match="no round"):
        await run_delete_round(db, slug=co.slug, amount=Decimal("77"))
    with pytest.raises(DeleteRoundError, match="provide --amount or --round-id"):
        await run_delete_round(db, slug=co.slug)


async def test_shared_primary_url_over_match_is_visible_and_bounded(
    db: AsyncSession,
) -> None:
    """Two rounds sharing a primary_news_url (reconcile's first-write-wins can
    produce this): deleting one round DOES purge the shared article — the
    over-match is deliberate and PREVIEWED in the dry-run's article_titles so
    the operator sees it before applying — but the sibling ROUND survives,
    its coverage link merely SET-NULLed."""
    shared = "https://shared.example.com/two-rounds-one-article"
    co = _co("shared-url-co")
    db.add(co)
    await db.flush()
    target = FundingRound(
        company_id=co.id,
        round_type="Series B",
        amount_raised=Decimal("50000000"),
        primary_news_url=shared,
    )
    sibling = FundingRound(
        company_id=co.id,
        round_type="Series A",
        amount_raised=Decimal("10000000"),
        primary_news_url=shared,
    )
    db.add_all([target, sibling])
    await db.flush()
    db.add(
        NewsArticle(
            company_id=co.id,
            url=shared,
            title="Shared Co raises again",
            source="shared.example.com",
            raw_content="Shared Co raised more money.",
            funding_round_id=sibling.id,
        )
    )
    await db.commit()

    preview = await run_delete_round(db, slug=co.slug, amount=Decimal("50000000"))
    assert preview.articles_deleted == 1  # the over-match is visible pre-apply

    await run_delete_round(db, slug=co.slug, amount=Decimal("50000000"), dry_run=False)
    rounds = (
        (await db.execute(select(FundingRound).where(FundingRound.company_id == co.id)))
        .scalars()
        .all()
    )
    assert [r.round_type for r in rounds] == ["Series A"]  # sibling survives
    articles = (
        (await db.execute(select(NewsArticle).where(NewsArticle.company_id == co.id)))
        .scalars()
        .all()
    )
    assert articles == []
