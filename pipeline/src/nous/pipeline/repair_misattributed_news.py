"""repair-misattributed-news — retroactive wrong-entity article/round purge.

The 2026-07-16 QA's aardvark class, applied to what is already stored:
articles attributed to a company whose name never actually appears in them.
Two production vectors put them there:

- keyword-scrape misattribution before (or around) the #116/#219 ingest
  guards: a ``"<name>" funding`` Google News query matched articles that
  merely USE a generic word ("Away" collected "diversify away from China
  will need funding"; "Aardvark" collected an Arthur-cartoon PBS story);
- the wrong-website gap-fill mining OTHER companies' announcements off a
  news site stored as the company homepage (helix carried Kinoa / Coval /
  ChatSee rounds whose articles never mention Helix — the same-host purge
  in repair-wrong-websites deliberately spares their cross-host sources;
  this stage owns them).

For every stored article of every non-excluded company, re-run the SAME
relevance guard ingest-news applies today (``article_mentions_company``,
including the #219 single-common-word funding-subject rule):

- publisher-URL rows pass their stored ``raw_content`` as the body (that is
  the fetched article text);
- Google-News-host rows pass no body (their ``raw_content`` is the headline
  + snippet fallback — the title is the signal, matching ingest semantics).

An article that fails for BOTH the company's current name and every
merged-away alias name (``slug_aliases`` de-slugified — a dedup survivor's
older coverage may reference the pre-merge name) is misattributed:

- the article row is deleted;
- any funding round whose ``primary_news_url`` is that article's URL is
  deleted too (it was extracted FROM the wrong-entity article, so it is some
  other company's round), with investor links cascading and
  ``funding_round_count`` refreshed.

NEVER deleted: articles that mention the name (however garbled the rest of
the row is — quality is another stage's job), and rounds whose primary
source is not among the deleted articles (e.g. rounds from the company's own
website). A round's deletion SET-NULLs sibling articles' funding_round_id;
those siblings mention the company (or they'd be deleted here too), so
nothing dangles.

Defaults to ``--dry-run`` (counts + a per-company example log, no writes) —
this stage is destructive and is meant to be measured on prod via the ops
lever before an apply. Idempotent: after an apply, every surviving article
mentions its company, so a re-run deletes nothing.
"""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle, SlugAlias
from nous.db.upsert import refresh_funding_round_count
from nous.sources.news import _GOOGLE_NEWS_HOST, article_mentions_company
from nous.util.url import hostname

logger = logging.getLogger(__name__)

# Cap the per-run examples carried in the summary (counts are never capped).
EXAMPLE_LIMIT: int = 25


class MisattributedExample(BaseModel):
    slug: str
    title: str
    round_deleted: bool


class RepairMisattributedNewsSummary(BaseModel):
    companies_seen: int = 0
    articles_checked: int = 0
    articles_deleted: int = 0
    rounds_deleted: int = 0
    companies_affected: int = 0
    examples: list[MisattributedExample] = Field(default_factory=list)
    dry_run: bool = True


def _alias_names(alias_slugs: list[str]) -> list[str]:
    """De-slugify merged-away slugs into name-shaped strings for the guard.

    "acme-labs-2f3a" → "acme labs 2f3a" — the disambiguator suffix tokens are
    harmless (they just never match), and the real name tokens are what the
    phrase check needs.
    """
    return [slug.replace("-", " ") for slug in alias_slugs if slug]


def _article_is_attributed(
    company_name: str,
    alias_names: list[str],
    *,
    title: str,
    url: str,
    raw_content: str | None,
) -> bool:
    """True when the article plausibly belongs to this company.

    Mirrors ingest semantics: a Google-News-host row's raw_content is the
    headline(+snippet) fallback, not a body — the title carries the signal.
    A publisher row's raw_content is the fetched article text and counts as
    the body. The current name OR any merged-away alias name may match.
    """
    is_gn = hostname(url) == _GOOGLE_NEWS_HOST
    body = None if is_gn else (raw_content or None)
    for name in (company_name, *alias_names):
        if article_mentions_company(name, title, body=body):
            return True
    return False


async def run_repair_misattributed_news(
    session: AsyncSession, *, dry_run: bool = True
) -> RepairMisattributedNewsSummary:
    """Purge stored articles (and their extracted rounds) that never mention
    their company. Per-company commit; idempotent."""
    summary = RepairMisattributedNewsSummary(dry_run=dry_run)

    companies = (
        await session.execute(
            select(Company.id, Company.name, Company.slug)
            .where(Company.exclusion_reason.is_(None))
            .order_by(Company.slug)
        )
    ).all()

    for company_id, company_name, slug in companies:
        summary.companies_seen += 1

        articles = (
            await session.execute(
                select(
                    NewsArticle.id,
                    NewsArticle.url,
                    NewsArticle.title,
                    NewsArticle.raw_content,
                )
                .where(NewsArticle.company_id == company_id)
                .order_by(NewsArticle.id)
            )
        ).all()
        if not articles:
            continue

        aliases = _alias_names(
            list(
                (
                    await session.execute(
                        select(SlugAlias.old_slug).where(
                            SlugAlias.company_id == company_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        )

        bad_ids: list[UUID] = []
        bad_urls: list[str] = []
        for article_id, url, title, raw_content in articles:
            summary.articles_checked += 1
            if _article_is_attributed(
                company_name,
                aliases,
                title=title,
                url=url,
                raw_content=raw_content,
            ):
                continue
            bad_ids.append(article_id)
            bad_urls.append(url)

        if not bad_ids:
            continue
        summary.companies_affected += 1

        # Rounds extracted FROM the misattributed articles.
        bad_round_ids = (
            (
                await session.execute(
                    select(FundingRound.id).where(
                        FundingRound.company_id == company_id,
                        FundingRound.primary_news_url.in_(bad_urls),
                    )
                )
            )
            .scalars()
            .all()
        )
        deleted_round_urls: set[str] = set()
        if bad_round_ids:
            deleted_round_urls = {
                url
                for url in (
                    await session.execute(
                        select(FundingRound.primary_news_url).where(
                            FundingRound.id.in_(bad_round_ids)
                        )
                    )
                )
                .scalars()
                .all()
                if url is not None
            }

        for _, url, title, _content in articles:
            if url not in bad_urls:
                continue
            if len(summary.examples) < EXAMPLE_LIMIT:
                summary.examples.append(
                    MisattributedExample(
                        slug=slug,
                        title=title[:100],
                        round_deleted=url in deleted_round_urls,
                    )
                )

        logger.info(
            "repair-misattributed-news: %s — %d article(s) never mention the "
            "company (%d extracted round(s) go with them)%s",
            slug,
            len(bad_ids),
            len(bad_round_ids),
            " [dry-run]" if dry_run else "",
        )
        summary.articles_deleted += len(bad_ids)
        summary.rounds_deleted += len(bad_round_ids)

        if dry_run:
            continue
        if bad_round_ids:
            await session.execute(
                delete(FundingRound).where(FundingRound.id.in_(bad_round_ids))
            )
        await session.execute(
            delete(NewsArticle).where(NewsArticle.id.in_(bad_ids))
        )
        await refresh_funding_round_count(session, company_id)
        await session.commit()

    return summary
