"""ingest-news pipeline stage.

For each company we already track, query Google News RSS for funding-related
articles in the lookback window and persist them to ``news_articles`` — storing
the funding headline (Google News links are opaque redirects whose body is
unreachable; see ``_GOOGLE_NEWS_HOST``). Then, if requested, sweep six
broad funding-news feeds (TechCrunch venture-tag, SiliconANGLE, PR Newswire,
Crunchbase News, VentureBeat, and GeekWire's funding tag) to catch funding
announcements for companies we don't yet have — asking the LLM to identify
the funded company from each headline + snippet and auto-creating a row.

Commit cadence: per-article so a mid-run crash leaves a clean state.

Idempotency: ``news_articles.url`` is unique on canonical form, so re-ingest
is a no-op via the upsert path (we use a pre-check rather than ON CONFLICT
because we don't want to fetch the article body if we already have it).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle
from nous.db.upsert import auto_create_company
from nous.llm.client import LLMError, LLMRateLimitError, complete_json
from nous.llm.prompts.news_company import HeadlineCompany, build_prompt
from nous.sources.crunchbase_news import fetch_crunchbase_news_funding_articles
from nous.sources.geekwire import fetch_geekwire_funding_articles
from nous.sources.news import (
    _GOOGLE_NEWS_HOST,
    NewsArticleResult,
    NewsClient,
    ResolvedArticle,
    article_mentions_company,
)
from nous.sources.prnewswire import fetch_prnewswire_funding_articles
from nous.sources.siliconangle import fetch_siliconangle_funding_articles
from nous.sources.techcrunch import fetch_techcrunch_funding_articles
from nous.sources.venturebeat import fetch_venturebeat_funding_articles
from nous.util.url import canonical_url, hostname

logger = logging.getLogger(__name__)

# Google News RSS <link>s are opaque redirect URLs (news.google.com/rss/articles/
# CBMi...). ``NewsClient.resolve_article`` chases that redirect to the real
# publisher and extracts the article body (Task A1), so extract-funding sees
# full prose instead of a one-line headline. When resolution fails (consent
# interstitial, robots-block, paywall stub, fetch error) we fall back to storing
# the funding headline (+ snippet), which still carries the core facts ("Ramp
# Raises Series F at $44 Billion Valuation") — enough for extract-funding, and
# never silently dropped as "thin". ``_GOOGLE_NEWS_HOST`` is imported from
# nous.sources.news (single source of truth).


async def _extract_company_from_tc_result(result: NewsArticleResult) -> str | None:
    """Use the LLM to identify the funded company from a TC feed item.

    Returns the clean company name, or None when the item isn't a funding
    announcement or the model can't identify a company. LLM exceptions
    (rate limit, parse failure) propagate to the caller, which decides
    whether to stop the sweep or skip the one item.
    """
    extraction = await complete_json(
        build_prompt(title=result.title, snippet=result.raw_content),
        HeadlineCompany,
    )
    if not extraction.is_funding_announcement:
        return None
    name = (extraction.company_name or "").strip()
    return name or None


class IngestNewsSummary(BaseModel):
    companies_queried: int = 0
    articles_seen: int = 0
    articles_kept: int = 0
    articles_inserted: int = 0
    articles_skipped_thin: int = 0
    # Per-company articles dropped by the relevance guard: the company name did
    # not actually appear in the headline / lede, so the keyword query matched
    # an unrelated article (e.g. a generic-named company like "Aardvark").
    articles_skipped_irrelevant: int = 0
    # Google-News redirects that resolved to a real publisher body (Task A1).
    articles_resolved: int = 0
    # Headline-only GN fallbacks skipped because the SAME title is already
    # stored for this company under another opaque GN URL (Google re-serves
    # one story with a fresh CBMi… token every sweep — the "MSN ×3" class,
    # 2026-07-16 QA).
    articles_skipped_duplicate_title: int = 0
    auto_created_companies: int = 0
    tc_skipped_no_company: int = 0


async def _article_already_stored(session: AsyncSession, url: str) -> bool:
    """Cheap existence check on the unique URL — avoids the body fetch."""
    stmt = select(exists().where(NewsArticle.url == url))
    return bool(await session.scalar(stmt))


async def _gn_title_already_stored(
    session: AsyncSession, company_id: UUID, title: str
) -> bool:
    """True when this company already has a Google-News-host article with the
    SAME title (case-insensitive, whitespace-trimmed).

    The news_articles.url uniqueness can't catch these: Google mints a fresh
    opaque /rss/articles/CBMi… URL for the same story on every sweep, so the
    identical headline (same outlet suffix and all) lands as a new row each
    time resolution to the publisher fails. Titles are compared only WITHIN
    one company and only for GN-host rows — two outlets covering the same
    event have different "- Outlet" suffixes and are genuinely distinct
    coverage, which the web timeline groups visually instead.
    """
    stmt = select(
        exists().where(
            NewsArticle.company_id == company_id,
            NewsArticle.url.like(f"https://{_GOOGLE_NEWS_HOST}/%"),
            func.lower(NewsArticle.title) == title.strip().lower(),
        )
    )
    return bool(await session.scalar(stmt))


def _headline_content(result: NewsArticleResult) -> str:
    """The funding-news content for a Google News result: headline (+ snippet).

    See ``_GOOGLE_NEWS_HOST``: the article body is unreachable behind Google's
    redirect, but the headline carries the company + round + amount/valuation.
    """
    title = result.title.strip()
    snippet = result.raw_content.strip()
    if snippet and snippet not in title:
        return f"{title}\n\n{snippet}"
    return title


async def _ingest_one_article(
    session: AsyncSession,
    client: NewsClient,
    company_id: UUID,
    company_name: str,
    result: NewsArticleResult,
    summary: IngestNewsSummary,
) -> None:
    """Persist one article to news_articles. Per-row commit.

    Google-News redirect URLs are RESOLVED to the real publisher first (Task
    A1): on success we store the publisher URL + source + full article body, so
    extract-funding gets rich text instead of a one-line headline. On resolution
    failure we fall back to storing the headline (+ snippet) — the funding facts
    survive and the row is never dropped as "thin". A direct publisher link
    (rare in GN RSS) still gets a plain body fetch with the thin-body guard.

    Relevance guard: the ``"<name>" funding`` query is ranked loosely by Google
    News, so for generic / common-word company names it returns articles that
    merely contain the word (the "Aardvark" biotech matched a PBS-funding story,
    a rugby fundraiser, etc.). Before storing, ``article_mentions_company``
    requires the company name to actually appear as a phrase in the headline (or
    the lede of the resolved body), dropping the misattribution. We pass the
    resolved/fetched body when we have one so a genuine article whose RSS title
    omits the exact name is still kept on a lede match; on the headline-only
    fallback the title is the sole signal (conservative — better to drop a
    borderline article than store an irrelevant one).
    """
    if await _article_already_stored(session, result.url):
        return

    url = result.url
    source = result.source
    content: str
    # The real article body when we obtained one (resolved publisher page or a
    # direct fetch); None on the Google-News headline-only fallback. The guard
    # only trusts a body match when a body actually exists.
    body_for_guard: str | None = None

    if hostname(result.url) == _GOOGLE_NEWS_HOST:
        resolved: ResolvedArticle | None = await client.resolve_article(result.url)
        if resolved is not None:
            # The redirect resolved to a real publisher article. Prefer the
            # publisher URL/source for attribution + dedup, and store the full
            # body. Guard against a publisher article already stored under
            # another Google-News link (or a direct link).
            if resolved.url != result.url and await _article_already_stored(
                session, resolved.url
            ):
                return
            url = resolved.url
            source = resolved.source
            content = resolved.body
            body_for_guard = resolved.body
            summary.articles_resolved += 1
        else:
            # Resolution failed — keep the headline (+ snippet); still useful.
            # But not twice: Google re-serves the same story under a fresh
            # opaque URL each sweep, so dedup headline-only rows by title.
            if await _gn_title_already_stored(session, company_id, result.title):
                summary.articles_skipped_duplicate_title += 1
                return
            content = _headline_content(result)
    else:
        body = await client.fetch_article_body(result.url)
        if body is None:
            summary.articles_skipped_thin += 1
            return
        content = body
        body_for_guard = body

    if not article_mentions_company(
        company_name,
        result.title,
        snippet=result.raw_content,
        body=body_for_guard,
    ):
        logger.info(
            "dropping article (company name %r not in headline/lede): %s",
            company_name,
            result.title,
        )
        summary.articles_skipped_irrelevant += 1
        return

    article = NewsArticle(
        company_id=company_id,
        url=url,
        title=result.title,
        source=source,
        published_date=result.published_date,
        raw_content=content,
    )
    session.add(article)
    await session.commit()
    summary.articles_inserted += 1


_DISCOVERED_VIA_BY_HOST: dict[str, str] = {
    "techcrunch.com": "techcrunch",
    "siliconangle.com": "siliconangle",
    "prnewswire.com": "prnewswire",
    "news.crunchbase.com": "crunchbase_news",
    "crunchbase.com": "crunchbase_news",
    "venturebeat.com": "venturebeat",
    "geekwire.com": "geekwire",
}


def _discovered_via_for_source(source_host: str) -> str:
    """Clean, stable ``discovered_via`` slug for a broad-feed article source.

    ``NewsArticleResult.source`` is a hostname (e.g. ``"techcrunch.com"``). The
    ``discovered_via`` column is a web filter/badge facet whose legacy broad-sweep
    value was the short alias ``"techcrunch"``. Map each known feed host to a
    clean slug so the facet stays consistent instead of splitting into hostname
    variants; unknown hosts degrade to the bare host (sans ``www.``).
    """
    host = source_host.lower().removeprefix("www.")
    return _DISCOVERED_VIA_BY_HOST.get(host, host)


async def run_ingest_news(
    session: AsyncSession,
    client: NewsClient,
    *,
    lookback_days: int = 7,
    include_techcrunch_broad: bool = True,
    max_companies: int | None = None,
    similarity_threshold: float = 0.85,
    funded_or_notable_only: bool = False,
) -> IngestNewsSummary:
    """Fetch per-company Google News results + optional broad-feed sweep.

    Per-company path: query Google News RSS for ``"<name>" funding``, filter
    to funding-keyword matches (done inside ``NewsClient.google_news_rss``),
    and persist the body for each new URL. Companies are taken least-recently
    -checked first (news_checked_at NULLS FIRST) and stamped on every attempt,
    so a ``max_companies``-bounded daily run rotates through the whole table
    every ~table/limit days — at the default workflow limit that matches the
    7-day lookback window, so no announcement is missed while the per-day
    request count to Google News stays small and polite.

    ``funded_or_notable_only=True`` narrows the per-company sweep to companies
    worth a long-lookback historical backfill (Task A3): those with at least
    one funding round (``funding_round_count > 0``) OR at least one existing
    ``news_articles`` row. CTO call: with no Company-level news-volume column,
    "already has news coverage" is the available "notable" proxy — the news
    pipeline surfaced them, so a multi-year sweep is high-yield — and pairing
    it with the funded set bounds the backfill's DeepSeek spend. Default False
    keeps the standing rotation (every non-excluded company) unchanged.

    Broad-feed path (``include_techcrunch_broad=True``): aggregate six
    funding-news feeds — TechCrunch venture tag, SiliconANGLE, PR Newswire
    VC feed, Crunchbase News, VentureBeat, and GeekWire's funding tag —
    dedup the combined list by canonical URL, then for each article whose
    title parses to a candidate company name, find-or-auto-create the
    company and persist the article.  All six sources are gated by the same
    flag so adding new feeds never changes the ``--no-techcrunch`` CLI
    behaviour.
    """
    summary = IngestNewsSummary()

    company_stmt = (
        select(Company)
        .where(Company.exclusion_reason.is_(None))
        .order_by(Company.news_checked_at.asc().nulls_first())
    )
    if funded_or_notable_only:
        company_stmt = company_stmt.where(
            or_(
                Company.funding_round_count > 0,
                exists().where(NewsArticle.company_id == Company.id),
            )
        )
    if max_companies is not None:
        company_stmt = company_stmt.limit(max_companies)
    company_result = await session.execute(company_stmt)
    companies = company_result.scalars().all()

    for company in companies:
        summary.companies_queried += 1
        query = f'"{company.name}" funding'
        try:
            results = await client.google_news_rss(query, lookback_days=lookback_days)
        except Exception:
            logger.exception("google_news_rss failed for %s", company.name)
            results = []
        for result in results:
            summary.articles_seen += 1
            # Keyword-filtered upstream; the relevance guard inside
            # _ingest_one_article may still drop it (articles_skipped_irrelevant).
            summary.articles_kept += 1
            await _ingest_one_article(
                session, client, company.id, company.name, result, summary
            )
        # Stamp the attempt (success or failure) so the rotation advances —
        # a head-of-line company whose query keeps failing must not pin the
        # whole rotation in place. Commit per company, matching the
        # per-article commits above.
        company.news_checked_at = datetime.now(tz=UTC)
        session.add(company)
        await session.commit()

    if include_techcrunch_broad:
        # --- Aggregate four broad funding-news feeds ---
        # Each fetch is independent: one failing source returns [] (see adapter
        # docstrings) and must not prevent the others from running. Sequential
        # awaits match the file's existing style and also respect the 1 req/s
        # per-domain throttle enforced inside NewsClient — parallel gather would
        # defeat that throttle for the same domain if any two feeds share a host.

        # 1. TechCrunch venture tag
        try:
            tc_results = await fetch_techcrunch_funding_articles(
                client, lookback_days=lookback_days
            )
        except Exception:
            logger.exception("TechCrunch venture feed fetch failed")
            tc_results = []

        # 2. SiliconANGLE
        try:
            sa_results = await fetch_siliconangle_funding_articles(
                client, lookback_days=lookback_days
            )
        except Exception:
            logger.exception("SiliconANGLE feed fetch failed")
            sa_results = []

        # 3. PR Newswire VC feed
        try:
            prn_results = await fetch_prnewswire_funding_articles(
                client, lookback_days=lookback_days
            )
        except Exception:
            logger.exception("PR Newswire VC feed fetch failed")
            prn_results = []

        # 4. Crunchbase News
        try:
            cb_results = await fetch_crunchbase_news_funding_articles(
                client, lookback_days=lookback_days
            )
        except Exception:
            logger.exception("Crunchbase News feed fetch failed")
            cb_results = []

        # 5. VentureBeat (main feed + keyword filter)
        try:
            vb_results = await fetch_venturebeat_funding_articles(
                client, lookback_days=lookback_days
            )
        except Exception:
            logger.exception("VentureBeat feed fetch failed")
            vb_results = []

        # 6. GeekWire funding tag
        try:
            gw_results = await fetch_geekwire_funding_articles(
                client, lookback_days=lookback_days
            )
        except Exception:
            logger.exception("GeekWire funding feed fetch failed")
            gw_results = []

        # Dedup by canonical URL before processing so the same article
        # surfaced by multiple feeds is processed exactly once. First
        # occurrence wins (TC → SA → PRN → CB → VB → GW priority).
        seen_urls: set[str] = set()
        broad_results: list[NewsArticleResult] = []
        for result in (
            tc_results + sa_results + prn_results + cb_results + vb_results + gw_results
        ):
            key = canonical_url(result.url)
            if key not in seen_urls:
                seen_urls.add(key)
                broad_results.append(result)

        for result in broad_results:
            summary.articles_seen += 1
            if await _article_already_stored(session, result.url):
                continue
            try:
                candidate = await _extract_company_from_tc_result(result)
            except LLMRateLimitError:
                # Stop making LLM calls for the rest of the sweep; the next run
                # picks up where this left off (unstored URLs are reprocessed).
                logger.warning(
                    "LLM rate limit hit during broad-feed company extraction — "
                    "stopping the broad sweep."
                )
                break
            except LLMError:
                logger.exception(
                    "LLM company extraction failed for broad-feed item %r", result.title
                )
                summary.tc_skipped_no_company += 1
                continue

            if candidate is None:
                summary.tc_skipped_no_company += 1
                continue
            # Fetch the body BEFORE auto-creating a company. A failed fetch
            # (robots block, 4xx, thin content) should not leave an orphan
            # company row with zero supporting articles. If the body succeeds
            # the URL is dedupable next run; if not, the worst case is one
            # wasted feed read per week.
            try:
                body = await client.fetch_article_body(result.url)
            except Exception:
                logger.exception(
                    "Broad-feed body fetch raised for %r (candidate=%r)",
                    result.title,
                    candidate,
                )
                continue
            if body is None:
                summary.articles_skipped_thin += 1
                continue
            try:
                company, created = await auto_create_company(
                    session,
                    name=candidate,
                    website=None,
                    # Tag discovery with a clean, stable per-source slug (e.g.
                    # "techcrunch", "siliconangle") instead of a hostname, so the
                    # discovered_via facet stays consistent with the legacy value
                    # rather than splitting into "techcrunch.com"-style variants.
                    discovered_via=_discovered_via_for_source(result.source),
                    similarity_threshold=similarity_threshold,
                )
                if created:
                    summary.auto_created_companies += 1
                summary.articles_kept += 1
                article = NewsArticle(
                    company_id=company.id,
                    url=result.url,
                    title=result.title,
                    source=result.source,
                    published_date=result.published_date,
                    raw_content=body,
                )
                session.add(article)
                await session.commit()
                summary.articles_inserted += 1
            except Exception:
                logger.exception(
                    "TC broad-ingest failed for %r (candidate=%r)",
                    result.title,
                    candidate,
                )
                await session.rollback()

    return summary
