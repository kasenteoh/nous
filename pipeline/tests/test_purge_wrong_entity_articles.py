"""Tests for purge-wrong-entity-articles — the per-company retroactive purge.

The wonder shape: stored pre-guard wrong-entity articles (which pass the
NAME-mention guard by construction) re-spawn purged rounds via
extract-funding. Pins: adjudicated purge of articles + linked/primary
rounds + total/status + kind-scoped ✓s; keepers survive; fail-KEEP on LLM
error; rate-limit aborts; dry-run writes nothing; no-description refused.
Requires DATABASE_URL; the LLM is monkeypatched at the guard seam.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification, FundingRound, NewsArticle
from nous.llm.client import LLMError, LLMRateLimitError
from nous.llm.prompts.article_subject_match import ArticleSubjectMatch
from nous.pipeline.purge_wrong_entity_articles import (
    PurgeWrongEntityError,
    run_purge_wrong_entity_articles,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_FOOD_URL = "https://prnewswire.example.com/wonder-650m"
_FOOD_URL_2 = "https://theinformation.example.com/wonder-ipo"
_REAL_URL = "https://edsurge.example.com/wonder-tutoring-30m"


def _co(slug: str, **kw: object) -> Company:
    return Company(
        name=slug.split("-")[0].title(),
        slug=slug,
        normalized_name=slug.split("-")[0],
        description_short=(
            "Wonder is an online education platform connecting students "
            "with expert tutors for personalized learning journeys."
        ),
        **kw,  # type: ignore[arg-type]
    )


async def _seed_wonder(db: AsyncSession) -> tuple[Company, FundingRound, FundingRound]:
    """Edtech-wonder carrying: a wrong round sourced from a stored food
    article (primary), a second UNLINKED food article, a REAL article +
    round, and a stated total + status sourced from the wrong articles."""
    co = _co(
        "wonder-purge-test",
        total_raised_usd=Decimal("650000000"),
        total_raised_source_url=_FOOD_URL_2,
        status="ipo",
        status_source_url=_FOOD_URL_2,
    )
    db.add(co)
    await db.flush()
    wrong_round = FundingRound(
        company_id=co.id,
        round_type="Series D",
        amount_raised=Decimal("650000000"),
        primary_news_url=_FOOD_URL,
    )
    real_round = FundingRound(
        company_id=co.id,
        round_type="Series A",
        amount_raised=Decimal("30000000"),
        primary_news_url=_REAL_URL,
    )
    db.add_all([wrong_round, real_round])
    await db.flush()
    db.add_all(
        [
            NewsArticle(
                company_id=co.id,
                url=_FOOD_URL,
                title="Wonder Announces $650 Million Series D Round",
                source="prnewswire.example.com",
                raw_content=(
                    "Wonder, the food hall and delivery startup founded by "
                    "Marc Lore, announced a $650M Series D."
                ),
                funding_round_id=wrong_round.id,
            ),
            NewsArticle(
                company_id=co.id,
                url=_FOOD_URL_2,
                title="Marc Lore's Wonder Ties $9 Billion Fundraise to IPO",
                source="theinformation.example.com",
                raw_content=(
                    "Wonder operates dozens of food halls serving meals from "
                    "celebrity chefs and eyes an IPO."
                ),
            ),
            NewsArticle(
                company_id=co.id,
                url=_REAL_URL,
                title="Wonder raises $30M to expand tutoring platform",
                source="edsurge.example.com",
                raw_content=(
                    "Wonder, the online education platform, raised $30M to "
                    "connect more students with expert tutors for "
                    "personalized learning."
                ),
                funding_round_id=real_round.id,
            ),
        ]
    )
    db.add_all(
        [
            FactVerification(
                company_id=co.id,
                fact_kind="funding_round",
                fact_ref=str(wrong_round.id),
                source_url=_FOOD_URL,
                claim="Wonder raised $650M.",
                verdict="supported",
                supporting_quote="$650 Million Series D",
                prompt_version="2026-07-17.1",
            ),
            FactVerification(
                company_id=co.id,
                fact_kind="total_raised",
                fact_ref="",
                source_url=_FOOD_URL_2,
                claim="Wonder has raised $650M total.",
                verdict="supported",
                supporting_quote="$9 Billion Fundraise",
                prompt_version="2026-07-17.1",
            ),
            FactVerification(
                company_id=co.id,
                fact_kind="funding_round",
                fact_ref=str(real_round.id),
                source_url=_REAL_URL,
                claim="Wonder raised $30M.",
                verdict="supported",
                supporting_quote="raised $30M",
                prompt_version="2026-07-17.1",
            ),
        ]
    )
    await db.commit()
    return co, wrong_round, real_round


def _adjudicate_by_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake LLM: food articles are the other Wonder; tutoring is ours."""

    async def _fake(prompt: str, schema: type) -> ArticleSubjectMatch:
        if "food hall" in prompt or "celebrity chefs" in prompt:
            return ArticleSubjectMatch(
                is_subject=False,
                confidence="high",
                other_entity_name="Wonder (food delivery)",
            )
        return ArticleSubjectMatch(is_subject=True, confidence="high")

    monkeypatch.setattr("nous.pipeline.entity_guard.complete_json", _fake)


async def test_dry_run_verdicts_without_writes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co, _, _ = await _seed_wonder(db)
    _adjudicate_by_content(monkeypatch)
    summary = await run_purge_wrong_entity_articles(db, slug=co.slug)
    assert summary.dry_run is True
    assert summary.articles_checked == 3
    assert summary.articles_purged == 2
    assert summary.rounds_purged == 1
    assert summary.total_raised_cleared and summary.status_reset
    assert summary.verifications_deleted == 2  # wrong round's + the total's
    assert {v.keep for v in summary.verdicts} == {True, False}

    # Nothing written.
    arts = (
        (await db.execute(select(NewsArticle).where(NewsArticle.company_id == co.id)))
        .scalars()
        .all()
    )
    assert len(arts) == 3
    await db.refresh(co)
    assert co.status == "ipo"


async def test_apply_purges_wrong_and_spares_real(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co, wrong_round, real_round = await _seed_wonder(db)
    _adjudicate_by_content(monkeypatch)
    summary = await run_purge_wrong_entity_articles(
        db, slug=co.slug, dry_run=False
    )
    assert summary.articles_purged == 2
    assert summary.rounds_purged == 1

    arts = (
        (await db.execute(select(NewsArticle).where(NewsArticle.company_id == co.id)))
        .scalars()
        .all()
    )
    assert [a.url for a in arts] == [_REAL_URL]
    rounds = (
        (await db.execute(select(FundingRound).where(FundingRound.company_id == co.id)))
        .scalars()
        .all()
    )
    assert [r.id for r in rounds] == [real_round.id]
    verifs = (
        (
            await db.execute(
                select(FactVerification).where(FactVerification.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert [v.fact_ref for v in verifs] == [str(real_round.id)]
    await db.refresh(co)
    assert co.total_raised_usd is None
    assert co.status == "active"
    assert co.funding_round_count == 1

    # Idempotent: the second run finds only the keeper.
    again = await run_purge_wrong_entity_articles(db, slug=co.slug, dry_run=False)
    assert again.articles_checked == 1
    assert again.articles_purged == 0


async def test_llm_error_keeps_article(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co, _, _ = await _seed_wonder(db)

    async def _flaky(prompt: str, schema: type) -> ArticleSubjectMatch:
        if "food hall" in prompt:
            raise LLMError("transient")
        return ArticleSubjectMatch(is_subject=True, confidence="high")

    monkeypatch.setattr("nous.pipeline.entity_guard.complete_json", _flaky)
    summary = await run_purge_wrong_entity_articles(db, slug=co.slug, dry_run=False)
    # Both food articles' prompts contain "food hall" -> both error -> KEPT;
    # the tutoring article adjudicates as a genuine match. Nothing purges.
    assert summary.articles_llm_error_kept == 2
    assert summary.articles_purged == 0
    assert summary.rounds_purged == 0
    arts = (
        (await db.execute(select(NewsArticle).where(NewsArticle.company_id == co.id)))
        .scalars()
        .all()
    )
    assert len(arts) == 3  # every article survives an errored run


async def test_kept_articles_round_survives_shared_primary_url(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review catch: a round whose primary_news_url is a DOOMED article's URL
    but which a KEPT article still links to is spared — and its primary URL
    is repointed to the survivor on apply. The kept article's URL must also
    never join the poison set for total/status clearing."""
    shared = "https://shared.example.com/wrong-copy"
    kept_url = "https://edsurge.example.com/right-copy"
    co = _co(
        "shared-purge-test",
        total_raised_usd=Decimal("30000000"),
        total_raised_source_url=kept_url,
    )
    db.add(co)
    await db.flush()
    r = FundingRound(
        company_id=co.id,
        round_type="Series A",
        amount_raised=Decimal("30000000"),
        primary_news_url=shared,
    )
    db.add(r)
    await db.flush()
    db.add_all(
        [
            NewsArticle(
                company_id=co.id,
                url=shared,
                title="Wonder food halls expand after raise",
                source="shared.example.com",
                raw_content=(
                    "Wonder, the food hall startup with celebrity chefs, "
                    "raised money."
                ),
                funding_round_id=r.id,
            ),
            NewsArticle(
                company_id=co.id,
                url=kept_url,
                title="Wonder raises $30M for tutoring",
                source="edsurge.example.com",
                raw_content=(
                    "Wonder, the online education platform, raised $30M to "
                    "connect students with expert tutors for personalized "
                    "learning."
                ),
                funding_round_id=r.id,
            ),
        ]
    )
    await db.commit()

    _adjudicate_by_content(monkeypatch)
    preview = await run_purge_wrong_entity_articles(db, slug=co.slug)
    assert preview.articles_purged == 1
    assert preview.rounds_purged == 0
    assert preview.rounds_repointed == 1
    assert preview.total_raised_cleared is False  # kept URL never poison

    summary = await run_purge_wrong_entity_articles(db, slug=co.slug, dry_run=False)
    assert summary.rounds_purged == 0
    rounds = (
        (await db.execute(select(FundingRound).where(FundingRound.company_id == co.id)))
        .scalars()
        .all()
    )
    assert len(rounds) == 1
    assert rounds[0].primary_news_url == kept_url  # repointed to the survivor
    await db.refresh(co)
    assert co.total_raised_usd == Decimal("30000000")


async def test_rate_limit_aborts_loudly(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co, _, _ = await _seed_wonder(db)

    async def _rl(prompt: str, schema: type) -> ArticleSubjectMatch:
        raise LLMRateLimitError("429")

    monkeypatch.setattr("nous.pipeline.entity_guard.complete_json", _rl)
    with pytest.raises(PurgeWrongEntityError, match="rate-limited"):
        await run_purge_wrong_entity_articles(db, slug=co.slug, dry_run=False)
    arts = (
        (await db.execute(select(NewsArticle).where(NewsArticle.company_id == co.id)))
        .scalars()
        .all()
    )
    assert len(arts) == 3  # nothing deleted


async def test_error_paths(db: AsyncSession) -> None:
    with pytest.raises(PurgeWrongEntityError, match="no company"):
        await run_purge_wrong_entity_articles(db, slug="does-not-exist")
    husk = Company(
        name="Husk",
        slug="husk-purge-test",
        normalized_name="husk",
        description_short=None,
    )
    db.add(husk)
    await db.commit()
    with pytest.raises(PurgeWrongEntityError, match="no description"):
        await run_purge_wrong_entity_articles(db, slug=husk.slug)
