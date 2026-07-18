"""delete-round — surgical deletion of ONE wrong-entity funding round.

The 2026-07-17 post-surgery QA sweep found rounds whose cited source is about
a DIFFERENT company that shares the name (bespoke-labs carrying IM8's $1B;
edtech-Wonder carrying food-Wonder's $650M). ``article_mentions_company``
passes these BY CONSTRUCTION (the article really does say "Wonder"), so the
retroactive purge can never catch them — and ``exclude-company`` is far too
blunt for a real company with one poisoned round. This stage is the missing
scalpel, dispatched per-round via ops.yml while the systemic entity-aware
ingestion guard is built.

Selection: ``--slug`` + ``--amount`` (the amount as stored, in whole USD).
If 2+ rounds match, the run FAILS listing each candidate's id — re-dispatch
with ``--round-id`` to disambiguate. Nothing is ever deleted on an ambiguous
match.

What one apply does, atomically (mirrors repair-duplicate-rounds' deletion
mechanics; every step is a no-op when not applicable):

- deletes the round (funding_round_investors cascade; sibling articles'
  ``funding_round_id`` SET-NULLs via the 0044 FK);
- with ``--purge-articles`` (default ON — the linked coverage is about the
  other company by definition of this lever's use case): deletes the round's
  linked articles AND the ``primary_news_url`` article row. NOTE the bounded
  recurrence window: a purged article inside the 14-day news lookback can be
  re-ingested and re-extracted until it ages out; the entity-aware ingest
  guard (the arc this lever belongs to) closes that permanently;
- clears ``companies.total_raised_usd/_source_url/_as_of`` when the stated
  total's source URL is the round's primary article or a purged article —
  the total came from the same wrong-entity source as the round. The
  ``--clear-total`` flag forces the clear when the total is sourced from a
  URL OUTSIDE the purge set (the bespoke-labs case: a different syndication
  URL of the same wrong-entity story dodges the URL match);
- resets ``companies.status`` to active (and clears ``status_source_url``)
  under the same source-URL match — a wrong-entity article must not leave a
  phantom "acquired"/"shut down" badge behind. ``--clear-status`` forces it
  for an out-of-purge-set source (the wave "shut down" case);
- deletes ``fact_verifications`` rows for the round (``fact_ref`` = round id)
  plus the company's total_raised / status verifications when those fields
  were cleared (a ✓ minted against the wrong-entity source must not survive
  the fact);
- refreshes ``funding_round_count`` and the ``latest_round_*`` denorms.

Dry-run by default: prints exactly what an apply would do, writes nothing.
Idempotent: after an apply the selection matches nothing.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification, FundingRound, NewsArticle
from nous.db.upsert import refresh_funding_round_count
from nous.pipeline.refresh_latest_round import refresh_latest_round

logger = logging.getLogger(__name__)


class DeleteRoundError(Exception):
    """Selection failed: no match, ambiguous match, or unknown company."""


class DeleteRoundSummary(BaseModel):
    slug: str
    round_id: str
    round_label: str
    articles_deleted: int = 0
    article_titles: list[str] = Field(default_factory=list)
    total_raised_cleared: bool = False
    status_reset: bool = False
    verifications_deleted: int = 0
    dry_run: bool = True


def _round_label(r: FundingRound) -> str:
    amount = f"${r.amount_raised:,.0f}" if r.amount_raised is not None else "—"
    return f"{r.round_type or 'round'}: {amount} ({r.announced_date or '—'})"


async def _select_round(
    session: AsyncSession,
    *,
    company_id: UUID,
    amount: Decimal | None,
    round_id: UUID | None,
) -> FundingRound:
    """Exactly one round or DeleteRoundError — never a guess."""
    if round_id is not None:
        row = await session.get(FundingRound, round_id)
        if row is None or row.company_id != company_id:
            raise DeleteRoundError(
                f"round {round_id} does not exist on this company"
            )
        return row
    if amount is None:
        raise DeleteRoundError("provide --amount or --round-id")
    rows = (
        (
            await session.execute(
                select(FundingRound).where(
                    FundingRound.company_id == company_id,
                    FundingRound.amount_raised == amount,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise DeleteRoundError(f"no round with amount_raised == {amount}")
    if len(rows) > 1:
        listing = "; ".join(f"{r.id} = {_round_label(r)}" for r in rows)
        raise DeleteRoundError(
            f"{len(rows)} rounds match that amount — re-dispatch with "
            f"--round-id to disambiguate: {listing}"
        )
    return rows[0]


async def run_delete_round(
    session: AsyncSession,
    *,
    slug: str,
    amount: Decimal | None = None,
    round_id: UUID | None = None,
    purge_articles: bool = True,
    clear_total: bool = False,
    clear_status: bool = False,
    dry_run: bool = True,
) -> DeleteRoundSummary:
    """Delete one round (and its wrong-entity side effects). See module doc."""
    company = (
        await session.execute(select(Company).where(Company.slug == slug))
    ).scalar_one_or_none()
    if company is None:
        raise DeleteRoundError(f"no company with slug {slug!r}")

    row = await _select_round(
        session, company_id=company.id, amount=amount, round_id=round_id
    )
    summary = DeleteRoundSummary(
        slug=slug, round_id=str(row.id), round_label=_round_label(row), dry_run=dry_run
    )

    # The article set the round's poison came from / points at: rows linked by
    # the 0044 FK plus the primary_news_url row (first-write-wins attribution).
    article_match = NewsArticle.funding_round_id == row.id
    if row.primary_news_url:
        article_match = or_(article_match, NewsArticle.url == row.primary_news_url)
    articles = (
        (
            await session.execute(
                select(NewsArticle).where(
                    NewsArticle.company_id == company.id, article_match
                )
            )
        )
        .scalars()
        .all()
        if purge_articles
        else []
    )
    summary.articles_deleted = len(articles)
    summary.article_titles = [a.title[:90] for a in articles]
    purged_urls = {a.url for a in articles}
    if row.primary_news_url:
        purged_urls.add(row.primary_news_url)

    # Stated total / status poisoned by the same source → clear with the round.
    # The explicit flags cover the same poison arriving via a source URL
    # OUTSIDE the purge set (a different syndication of the wrong-entity
    # story); they still no-op when there is nothing to clear.
    if (
        company.total_raised_source_url and company.total_raised_source_url in purged_urls
    ) or (clear_total and company.total_raised_usd is not None):
        summary.total_raised_cleared = True
    if company.status not in (None, "active") and (
        clear_status
        or (company.status_source_url and company.status_source_url in purged_urls)
    ):
        summary.status_reset = True

    verif_where = [
        FactVerification.company_id == company.id,
        FactVerification.fact_kind == "funding_round",
        FactVerification.fact_ref == str(row.id),
    ]
    verif_count = len(
        (await session.execute(select(FactVerification.id).where(*verif_where)))
        .scalars()
        .all()
    )
    # Company-level fact kinds whose field is being cleared: their ✓ rows go
    # with the fact (a verification of a wrong-entity claim must not survive).
    cleared_kinds: list[str] = []
    if summary.total_raised_cleared:
        cleared_kinds.append("total_raised")
    if summary.status_reset:
        cleared_kinds.append("status")
    company_verif_count = 0
    if cleared_kinds:
        company_verif_count = len(
            (
                await session.execute(
                    select(FactVerification.id).where(
                        FactVerification.company_id == company.id,
                        FactVerification.fact_kind.in_(cleared_kinds),
                    )
                )
            )
            .scalars()
            .all()
        )
    summary.verifications_deleted = verif_count + company_verif_count

    logger.info(
        "delete-round%s: %s — %s | articles=%d total_cleared=%s status_reset=%s "
        "verifications=%d",
        " (dry-run)" if dry_run else "",
        slug,
        summary.round_label,
        summary.articles_deleted,
        summary.total_raised_cleared,
        summary.status_reset,
        summary.verifications_deleted,
    )
    if dry_run:
        return summary

    for article in articles:
        await session.delete(article)
    await session.execute(
        delete(FactVerification).where(*verif_where)
    )
    if cleared_kinds:
        await session.execute(
            delete(FactVerification).where(
                FactVerification.company_id == company.id,
                FactVerification.fact_kind.in_(cleared_kinds),
            )
        )
    if summary.total_raised_cleared:
        company.total_raised_usd = None
        company.total_raised_source_url = None
        company.total_raised_as_of = None
    if summary.status_reset:
        company.status = "active"
        company.status_source_url = None
    await session.delete(row)
    await session.flush()
    await refresh_funding_round_count(session, company.id)
    # Full-table recompute, deliberately: a company-scoped variant would
    # duplicate the DISTINCT ON logic for a ~3.2k-row table whose two set
    # UPDATEs take milliseconds, and this lever runs serialized in the
    # nous-pipeline-db concurrency group (no concurrent writer to lock out).
    await refresh_latest_round(session)
    await session.commit()
    return summary
