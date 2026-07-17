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
the row is — quality is another stage's job); rounds whose primary source is
not among the deleted articles (e.g. rounds from the company's own website);
and rounds a SURVIVING article still links to via funding_round_id —
reconcile's first-write-wins primary_news_url means a bad-article-created
round may have been independently confirmed by a good article later; such a
round is kept with its primary source repointed to the survivor. A round's
deletion SET-NULLs sibling articles' funding_round_id; those siblings
mention the company (or they'd be deleted here too), so nothing dangles.

Defaults to ``--dry-run`` (counts + a per-company example log, no writes) —
this stage is destructive and is meant to be measured on prod via the ops
lever before an apply. Idempotent: after an apply, every surviving article
mentions its company, so a re-run deletes nothing.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle, SlugAlias
from nous.db.upsert import refresh_funding_round_count
from nous.sources.news import (
    _COMMON_NAME_WORDS,
    _GOOGLE_NEWS_HOST,
    _company_name_tokens,
    article_mentions_company,
)
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


# slug_with_disambiguator appends "-" + 6 hex chars on collisions. That token
# never appears in article text, so a de-slugified disambiguated alias
# ("acme labs a3f9c2") would be a phrase that can NEVER match — the alias
# safety net silently dead (review catch). Both variants are tried: with the
# suffix stripped AND raw, because a real word can be all-hex ("decade") and
# keeping both only ever widens acceptance — the safe direction for a purge.
_DISAMBIGUATOR_RE = re.compile(r"-[0-9a-f]{6}$")

# Corporate suffixes the shared strip_corporate_suffix does NOT cover (adding
# them there would change normalize_name — every stored match key — so the
# purge handles them locally). A name like "Anthropic PBC" tokenizes to
# ["anthropic", "pbc"], a 2-token phrase no headline ever contains; trying the
# suffix-less variant too keeps those articles safe. False-keep direction only.
_EXTRA_SUFFIX_TOKENS: frozenset[str] = frozenset(
    {"pbc", "gmbh", "bv", "sa", "ag", "plc", "pty", "oy", "ab", "sarl"}
)


def _alias_names(alias_slugs: list[str]) -> list[str]:
    """De-slugify merged-away slugs into name-shaped strings for the guard."""
    names: list[str] = []
    for slug in alias_slugs:
        if not slug:
            continue
        names.append(slug.replace("-", " "))
        stripped = _DISAMBIGUATOR_RE.sub("", slug)
        if stripped != slug and stripped:
            names.append(stripped.replace("-", " "))
    return names


def _name_variants(company_name: str) -> list[str]:
    """The name plus a purge-local variant without a trailing foreign/PBC
    suffix token the shared stripper doesn't know."""
    variants = [company_name]
    tokens = company_name.strip().split()
    if len(tokens) >= 2 and tokens[-1].strip(".").lower() in _EXTRA_SUFFIX_TOKENS:
        variants.append(" ".join(tokens[:-1]))
    return variants


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

    Deletion is costlier than a kept borderline article, so the purge accepts
    TWO shapes the strict ingest guard rejects (2026-07-17 prod dry-run
    precision review — these were the only false-flag classes found):

    - the SQUASHED name as one token — coverage writes "PhysicsWallah" where
      the row says "Physics Wallah" (2 real rounds would have died);
    - for multi-token names, a DISTINCTIVE head token alone — "Genesis raises
      $200M" for Genesis Therapeutics, "Cato's Shlomo Kramer …" for Cato
      Networks. Distinctive = ≥4 chars and not a common dictionary word, so
      "Away"/"Key"-class heads never qualify and single-token names keep the
      calibrated strict rules ("musically" must NOT be spared by the
      "- Music Ally" outlet suffix).
    """
    is_gn = hostname(url) == _GOOGLE_NEWS_HOST
    body = None if is_gn else (raw_content or None)
    for name in (*_name_variants(company_name), *alias_names):
        if article_mentions_company(name, title, body=body):
            return True
        tokens = _company_name_tokens(name)
        if len(tokens) < 2:
            continue
        # Squashed-name variant ("physics wallah" → "physicswallah").
        squashed = "".join(tokens)
        if article_mentions_company(squashed, title, body=body):
            return True
        # Distinctive-head-token spare.
        head = tokens[0]
        if (
            len(head) >= 4
            and head not in _COMMON_NAME_WORDS
            and article_mentions_company(head, title, body=body)
        ):
            return True
    return False


async def run_repair_misattributed_news(
    session: AsyncSession, *, dry_run: bool = True
) -> RepairMisattributedNewsSummary:
    """Purge stored articles (and their extracted rounds) that never mention
    their company. Per-company commit; idempotent."""
    summary = RepairMisattributedNewsSummary(dry_run=dry_run)

    # Batch-load EVERYTHING up front (three queries total), then scan in pure
    # Python. The per-company query loop version did ~3 round-trips × every
    # company against the Supabase pooler and blew ops.yml's 10-minute job
    # timeout before finishing the letter "a" (2026-07-17 prod dry-run).
    companies = (
        await session.execute(
            select(Company.id, Company.name, Company.slug)
            .where(Company.exclusion_reason.is_(None))
            .order_by(Company.slug)
        )
    ).all()
    shown_ids = {row.id for row in companies}

    articles_by_company: dict[UUID, list[tuple[UUID, str, str, str | None]]] = (
        defaultdict(list)
    )
    for aid, a_company_id, url, title, raw_content in (
        await session.execute(
            select(
                NewsArticle.id,
                NewsArticle.company_id,
                NewsArticle.url,
                NewsArticle.title,
                NewsArticle.raw_content,
            ).order_by(NewsArticle.id)
        )
    ).all():
        if a_company_id in shown_ids:
            articles_by_company[a_company_id].append(
                (aid, url, title, raw_content)
            )

    aliases_by_company: dict[UUID, list[str]] = defaultdict(list)
    for old_slug, alias_company_id in (
        await session.execute(select(SlugAlias.old_slug, SlugAlias.company_id))
    ).all():
        aliases_by_company[alias_company_id].append(old_slug)

    for company_id, company_name, slug in companies:
        summary.companies_seen += 1

        articles = articles_by_company.get(company_id, [])
        if not articles:
            continue

        aliases = _alias_names(aliases_by_company.get(company_id, []))

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

        # Rounds extracted FROM the misattributed articles. Exception (review
        # catch): reconcile's first-write-wins primary_news_url means a round
        # first created from the bad article may have been independently
        # CONFIRMED by a good article that reconciled into it later — a
        # surviving article's funding_round_id link is that evidence. Such a
        # round is kept, with its primary_news_url repointed to the surviving
        # linked article so the citation doesn't dangle after the bad article
        # is deleted.
        candidate_round_ids = (
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
        bad_round_ids: list[UUID] = []
        for round_id in candidate_round_ids:
            surviving_link_url = (
                await session.execute(
                    select(NewsArticle.url)
                    .where(
                        NewsArticle.funding_round_id == round_id,
                        NewsArticle.id.not_in(bad_ids),
                    )
                    .order_by(NewsArticle.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if surviving_link_url is None:
                bad_round_ids.append(round_id)
                continue
            logger.info(
                "repair-misattributed-news: %s — keeping round %s (confirmed "
                "by a surviving article); repointing its primary source",
                slug,
                round_id,
            )
            if not dry_run:
                round_row = await session.get(FundingRound, round_id)
                if round_row is not None:
                    round_row.primary_news_url = surviving_link_url
                    session.add(round_row)
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
            "company (%d extracted round(s) go with them)%s: %s",
            slug,
            len(bad_ids),
            len(bad_round_ids),
            " [dry-run]" if dry_run else "",
            "; ".join(
                title[:90]
                for _, url, title, _c in articles
                if url in bad_urls
            )[:600],
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
