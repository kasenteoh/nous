"""purge-wrong-entity-articles — per-company retroactive entity purge (ops).

The #235 ingest guard closes the FAUCET, but wrong-entity articles STORED
before it exist upstream of extract-funding, which re-mines rounds from them
every cron: hours after the 2026-07-18 re-heal, wonder's $650M Series D
re-spawned from a pre-guard prnewswire article (one of TEN stored
food-Wonder articles the round purges never touched — delete-round only
reaches round-LINKED articles, and repair-misattributed-news only reaches
articles that fail the NAME-mention guard, which these pass by
construction).

This lever runs the SAME decision the ingest guard applies — cheap
calibrated corroboration signals, then LLM adjudication
(``article_subject_match``) — over EVERY stored article of ONE company:

- articles whose subject is NOT this company are deleted;
- rounds sourced from a purged article (0044 ``funding_round_id`` link OR
  ``primary_news_url`` match) are deleted with their ✓ verifications —
  mirroring delete-round's mechanics;
- a stated total / non-active status sourced from a purged URL is cleared
  (its ✓ dies too);
- denorms (``funding_round_count``, ``latest_round_*``) refresh after.

Fail-KEEP semantics per article: an LLM error keeps the article (a later
run retries — never delete on an unread verdict); a 429 aborts the run
loudly (idempotent — re-dispatch when the limiter clears). A company with
no description cannot be adjudicated and is refused (enrich it first, or
use exclude-company / delete-round directly).

Dry-run by default: prints every article's verdict (title, reason, the
other entity when named) so the operator reviews exactly what an apply
deletes. This is also the per-company unit of the retroactive entity audit
(the probe's suspect list from #232–#234 is the dispatch queue).

Cost: one adjudication per stored non-strong article (~$0.0005 each; a
company page holds at most a few dozen articles).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification, FundingRound, NewsArticle
from nous.db.upsert import refresh_funding_round_count
from nous.pipeline.entity_guard import check_article_entity
from nous.pipeline.refresh_latest_round import refresh_latest_round

logger = logging.getLogger(__name__)


class PurgeWrongEntityError(Exception):
    """Unknown company, no description to adjudicate against, or rate limit."""


class ArticleVerdict(BaseModel):
    title: str
    url: str
    keep: bool
    reason: str
    other_entity: str | None = None


class PurgeWrongEntitySummary(BaseModel):
    slug: str
    articles_checked: int = 0
    articles_purged: int = 0
    articles_kept: int = 0
    articles_llm_error_kept: int = 0
    rounds_purged: int = 0
    round_labels: list[str] = Field(default_factory=list)
    total_raised_cleared: bool = False
    status_reset: bool = False
    verifications_deleted: int = 0
    verdicts: list[ArticleVerdict] = Field(default_factory=list)
    dry_run: bool = True


def _round_label(r: FundingRound) -> str:
    amount = f"${r.amount_raised:,.0f}" if r.amount_raised is not None else "—"
    return f"{r.round_type or 'round'}: {amount} ({r.announced_date or '—'})"


async def run_purge_wrong_entity_articles(
    session: AsyncSession,
    *,
    slug: str,
    dry_run: bool = True,
) -> PurgeWrongEntitySummary:
    """Adjudicate every stored article of ``slug``; purge the wrong-entity
    ones and everything sourced from them. See module doc."""
    company = (
        await session.execute(select(Company).where(Company.slug == slug))
    ).scalar_one_or_none()
    if company is None:
        raise PurgeWrongEntityError(f"no company with slug {slug!r}")
    if not (company.description_short or "").strip():
        raise PurgeWrongEntityError(
            f"{slug!r} has no description to adjudicate against — enrich it "
            "first, or use exclude-company / delete-round directly"
        )

    articles = (
        (
            await session.execute(
                select(NewsArticle).where(NewsArticle.company_id == company.id)
            )
        )
        .scalars()
        .all()
    )
    summary = PurgeWrongEntitySummary(slug=slug, dry_run=dry_run)
    doomed: list[NewsArticle] = []
    for article in articles:
        summary.articles_checked += 1
        decision = await check_article_entity(
            company, title=article.title, text=article.raw_content or ""
        )
        if decision.rate_limited:
            raise PurgeWrongEntityError(
                "LLM rate-limited mid-run — aborting (idempotent; re-dispatch "
                f"when the limiter clears; {summary.articles_checked - 1} of "
                f"{len(articles)} articles already adjudicated this run)"
            )
        if decision.llm_error:
            # Fail-KEEP: never delete on an unread verdict.
            summary.articles_kept += 1
            summary.articles_llm_error_kept += 1
            summary.verdicts.append(
                ArticleVerdict(
                    title=article.title[:110],
                    url=article.url,
                    keep=True,
                    reason="llm-error (kept; retried next run)",
                )
            )
            continue
        keep = decision.attach
        summary.verdicts.append(
            ArticleVerdict(
                title=article.title[:110],
                url=article.url,
                keep=keep,
                reason=decision.reason,
                other_entity=decision.other_entity,
            )
        )
        if keep:
            summary.articles_kept += 1
        else:
            summary.articles_purged += 1
            doomed.append(article)

    purged_urls = {a.url for a in doomed}
    rounds = (
        (
            await session.execute(
                select(FundingRound).where(FundingRound.company_id == company.id)
            )
        )
        .scalars()
        .all()
    )
    doomed_rounds = [
        r
        for r in rounds
        if (r.primary_news_url and r.primary_news_url in purged_urls)
        or any(
            a.funding_round_id == r.id for a in doomed
        )
    ]
    summary.rounds_purged = len(doomed_rounds)
    summary.round_labels = [_round_label(r) for r in doomed_rounds]
    for r in doomed_rounds:
        if r.primary_news_url:
            purged_urls.add(r.primary_news_url)

    if (
        company.total_raised_source_url
        and company.total_raised_source_url in purged_urls
    ):
        summary.total_raised_cleared = True
    if (
        company.status not in (None, "active")
        and company.status_source_url
        and company.status_source_url in purged_urls
    ):
        summary.status_reset = True

    # ✓ rows that die: each purged round's, plus company-level kinds whose
    # field is being cleared (mirrors delete-round).
    verif_conditions: list[ColumnElement[bool]] = [
        (FactVerification.fact_kind == "funding_round")
        & FactVerification.fact_ref.in_([str(r.id) for r in doomed_rounds])
    ]
    cleared_kinds: list[str] = []
    if summary.total_raised_cleared:
        cleared_kinds.append("total_raised")
    if summary.status_reset:
        cleared_kinds.append("status")
    if cleared_kinds:
        verif_conditions.append(FactVerification.fact_kind.in_(cleared_kinds))
    verif_where = (
        FactVerification.company_id == company.id,
        or_(*verif_conditions),
    )
    if doomed_rounds or cleared_kinds:
        summary.verifications_deleted = len(
            (await session.execute(select(FactVerification.id).where(*verif_where)))
            .scalars()
            .all()
        )

    logger.info(
        "purge-wrong-entity-articles%s: %s — %d/%d articles purged, %d rounds, "
        "total_cleared=%s status_reset=%s verifications=%d llm_errors=%d",
        " (dry-run)" if dry_run else "",
        slug,
        summary.articles_purged,
        summary.articles_checked,
        summary.rounds_purged,
        summary.total_raised_cleared,
        summary.status_reset,
        summary.verifications_deleted,
        summary.articles_llm_error_kept,
    )
    if dry_run:
        return summary

    for article in doomed:
        await session.delete(article)
    if doomed_rounds or cleared_kinds:
        await session.execute(delete(FactVerification).where(*verif_where))
    for r in doomed_rounds:
        await session.delete(r)
    if summary.total_raised_cleared:
        company.total_raised_usd = None
        company.total_raised_source_url = None
        company.total_raised_as_of = None
    if summary.status_reset:
        company.status = "active"
        company.status_source_url = None
    await session.flush()
    await refresh_funding_round_count(session, company.id)
    # Full-table recompute, same rationale as delete-round: serialized in the
    # nous-pipeline-db concurrency group, milliseconds on a ~3-4k-row table.
    await refresh_latest_round(session)
    await session.commit()
    return summary
