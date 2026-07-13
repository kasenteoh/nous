"""resolve-website-fallback — resolve husk websites by re-mining, not re-scraping.

The ~890 website-less "husk" companies have no resolvable website because the
homepage scrape gets Cloudflare-403'd from GitHub Actions datacenter IPs (both
httpx and the curl_cffi impersonation fallback). This stage does **not** fight
Cloudflare — proxy/evasion is rejected on principle (ROADMAP "route around,
don't evade"). It resolves the website from sources that were **never the origin
homepage**, in priority order, stopping at the first accepted candidate:

1. **wikidata**      — Wikidata "official website" (P856) for a name+org-type
   matched entity. Free, un-Cloudflared, and prominent companies (which husks
   are) are exactly who's indexed. Highest precision.
2. **news_outbound** — the company's own homepage link in the body of a news
   article we already sourced about it. Re-fetches the *article* (not the
   Cloudflare-origin homepage) and extracts the matching outbound link.

Because the origin homepage is unreachable (that's the whole problem), a
candidate is **not** fetch-validated — doing so would 403 and reject good
candidates. Instead each source applies its own entity-match gates and the
resolved website is recorded with a provenance source (``website_source`` +
``website_source_url``), so every resolution is attributable and reversible
(a wrong site is caught later by repair-wrong-websites, which appends it to
``rejected_urls`` — respected here so it's never re-picked).

Idempotent + self-bounding: selection keys on ``website IS NULL`` and the
stage's own ``website_fallback_checked_at`` back-off stamp, prominence-ordered,
per-company commit, ``--limit`` / ``--max-runtime-minutes`` bounded. ``--dry-run``
runs *every* source per company (not first-hit) and writes nothing, so the yield
table can report per-source hit rate and cross-source conflicts. $0 — Wikidata
and news re-fetches are free (DeepSeek, the only paid line, is untouched).
"""

from __future__ import annotations

import logging
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from nous.db.models import Company, NewsArticle
from nous.sources.article_links import extract_outbound_links, select_company_link
from nous.sources.news import NewsClient, RobotsBlockedError
from nous.sources.reject_hosts import is_aggregator_url
from nous.sources.wikidata import WikidataClient
from nous.util.ssrf import BlockedAddressError
from nous.util.url import canonical_domain, hostname, is_storable_website

logger = logging.getLogger(__name__)

SOURCE_WIKIDATA = "wikidata"
SOURCE_NEWS_OUTBOUND = "news_outbound"
# Priority order: highest-precision source first (first accepted wins in apply).
DEFAULT_SOURCES: tuple[str, ...] = (SOURCE_WIKIDATA, SOURCE_NEWS_OUTBOUND)

# How many of a husk's most-recent articles to re-fetch when mining links. A
# small cap keeps the per-company request budget bounded; the subject's homepage
# link, if present, is almost always in the most recent coverage.
_MAX_ARTICLES_PER_COMPANY = 3


class SourceStat(BaseModel):
    """Per-source tally for the yield table."""

    source: str
    attempted: int = 0  # companies this source was run against
    candidate_found: int = 0  # produced a raw candidate URL
    accepted: int = 0  # candidate passed the shared gates


class CompanyResolution(BaseModel):
    """One company's outcome — the dry-run audit / review row."""

    slug: str
    name: str
    resolved_website: str | None = None
    source: str | None = None
    source_url: str | None = None
    conflict: bool = False  # ≥2 sources produced different-domain candidates
    candidates: dict[str, str] = Field(default_factory=dict)  # source → candidate


class ResolveWebsiteFallbackSummary(BaseModel):
    """Stage summary — feeds record_pipeline_run and the yield table."""

    dry_run: bool
    companies_seen: int = 0
    resolved: int = 0  # got (apply) / would get (dry) a website
    missed: int = 0  # no source produced an accepted candidate
    errors: int = 0
    conflicts: int = 0
    stopped_early: bool = False
    per_source: list[SourceStat] = Field(default_factory=list)
    resolutions: list[CompanyResolution] = Field(default_factory=list)


class _Candidate(BaseModel):
    source: str
    website: str  # accepted, storable origin URL
    source_url: str  # provenance citation


async def _wikidata_candidate(
    client: WikidataClient, company: Company
) -> _Candidate | None:
    match = await client.official_website(company.name)
    if match is None:
        return None
    return _Candidate(
        source=SOURCE_WIKIDATA, website=match.website, source_url=match.entity_url
    )


async def _news_candidate(
    client: NewsClient,
    company: Company,
    articles: list[NewsArticle],
    rejected_domains: frozenset[str],
) -> _Candidate | None:
    for article in articles:
        try:
            html = await client.fetch_text(article.url)
        except (
            RobotsBlockedError,
            httpx.HTTPStatusError,
            httpx.RequestError,
            BlockedAddressError,
        ) as exc:
            # Any fetch failure (robots, HTTP, network, SSRF) → try the next
            # article. News fetches are best-effort; a miss here is not a stage
            # error, it just means this source found nothing for this company.
            logger.info("news re-fetch failed for %s: %s", article.url, exc)
            continue
        links = extract_outbound_links(html, article.url)
        publisher = article.source or hostname(article.url)
        candidate = select_company_link(
            links,
            company.name,
            publisher_host=publisher,
            rejected_domains=rejected_domains,
        )
        if candidate is not None:
            return _Candidate(
                source=SOURCE_NEWS_OUTBOUND, website=candidate, source_url=article.url
            )
    return None


def _accept(candidate: _Candidate, rejected_domains: frozenset[str]) -> bool:
    """Shared final gates applied to any source's candidate (defense in depth)."""
    if not is_storable_website(candidate.website):
        return False
    if is_aggregator_url(candidate.website):
        return False
    domain = canonical_domain(candidate.website)
    return domain is not None and domain not in rejected_domains


async def _recent_articles(
    session: AsyncSession, company: Company
) -> list[NewsArticle]:
    stmt = (
        select(NewsArticle)
        .where(NewsArticle.company_id == company.id)
        .order_by(
            NewsArticle.published_date.desc().nulls_last(),
            NewsArticle.created_at.desc(),
        )
        .limit(_MAX_ARTICLES_PER_COMPANY)
    )
    return list((await session.execute(stmt)).scalars().all())


async def run_resolve_website_fallback(
    session: AsyncSession,
    *,
    user_agent: str,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    refetch_after_days: int = 90,
    limit: int | None = None,
    max_runtime_minutes: float | None = None,
    dry_run: bool = False,
) -> ResolveWebsiteFallbackSummary:
    """Resolve website-less husks from non-origin sources.

    Selection mirrors resolve-homepages (``website IS NULL`` + not excluded,
    prominence-ordered) but keys back-off on this stage's own
    ``website_fallback_checked_at`` stamp, so it rotates independently of the
    TLD-guessing resolver. On a hit: set ``website`` + provenance +
    ``website_resolved_at`` + the stamp. On a miss: set only the stamp (so the
    ``refetch_after_days`` window suppresses re-hammering). On a transient error:
    stamp nothing (stays eligible next run). ``--dry-run`` runs every source and
    writes nothing.
    """
    summary = ResolveWebsiteFallbackSummary(dry_run=dry_run)
    stats: dict[str, SourceStat] = {s: SourceStat(source=s) for s in sources}
    started = time.monotonic()
    deadline = (
        started + max_runtime_minutes * 60 if max_runtime_minutes is not None else None
    )
    cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)

    stmt = (
        select(Company)
        .where(
            Company.website.is_(None),
            Company.exclusion_reason.is_(None),
            or_(
                Company.website_fallback_checked_at.is_(None),
                Company.website_fallback_checked_at < cutoff,
            ),
        )
        # Prominence-first (raise desc, rounds desc, id) so a bounded --limit
        # resolves marquee husks before the long tail. Mirrors resolve-homepages.
        .order_by(
            Company.latest_round_amount.desc().nulls_last(),
            Company.funding_round_count.desc(),
            Company.id,
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    companies = list((await session.execute(stmt)).scalars().all())

    use_wikidata = SOURCE_WIKIDATA in sources
    use_news = SOURCE_NEWS_OUTBOUND in sources

    async with AsyncExitStack() as stack:
        wd: WikidataClient | None = None
        news: NewsClient | None = None
        if use_wikidata:
            wd = await stack.enter_async_context(WikidataClient(user_agent))
        if use_news:
            news = await stack.enter_async_context(NewsClient(user_agent))

        for company in companies:
            if deadline is not None and time.monotonic() >= deadline:
                summary.stopped_early = True
                logger.info(
                    "resolve-website-fallback: %.0f-min budget reached after %d "
                    "companies — stopping (%d left)",
                    max_runtime_minutes or 0,
                    summary.companies_seen,
                    len(companies) - summary.companies_seen,
                )
                break

            summary.companies_seen += 1
            rejected_domains = frozenset(
                d
                for d in (canonical_domain(u) for u in (company.rejected_urls or []))
                if d is not None
            )
            resolution = CompanyResolution(slug=company.slug, name=company.name)

            # Gather candidates. Apply mode stops at the first accepted source;
            # dry-run runs every source to measure per-source yield + conflicts.
            winner: _Candidate | None = None
            try:
                articles: list[NewsArticle] | None = None
                for source in sources:
                    stats[source].attempted += 1
                    candidate: _Candidate | None = None
                    if source == SOURCE_WIKIDATA and wd is not None:
                        candidate = await _wikidata_candidate(wd, company)
                    elif source == SOURCE_NEWS_OUTBOUND and news is not None:
                        if articles is None:
                            articles = await _recent_articles(session, company)
                        candidate = await _news_candidate(
                            news, company, articles, rejected_domains
                        )
                    if candidate is None:
                        continue
                    stats[source].candidate_found += 1
                    resolution.candidates[source] = candidate.website
                    if not _accept(candidate, rejected_domains):
                        continue
                    stats[source].accepted += 1
                    if winner is None:
                        winner = candidate
                    if not dry_run:
                        break
            except Exception:  # noqa: BLE001
                # Unexpected error mid-company (not the swallowed per-source
                # misses) — count it and leave the stamp untouched so the row
                # stays eligible next run. Never sink the whole stage.
                logger.exception("resolve-website-fallback failed for %s", company.slug)
                summary.errors += 1
                continue

            # Cross-source conflict: ≥2 sources produced different-domain
            # candidates → at least one is wrong (a free wrong-site signal).
            distinct_domains = {
                canonical_domain(u) for u in resolution.candidates.values()
            }
            distinct_domains.discard(None)
            if len(distinct_domains) > 1:
                resolution.conflict = True
                summary.conflicts += 1

            if winner is not None:
                summary.resolved += 1
                resolution.resolved_website = winner.website
                resolution.source = winner.source
                resolution.source_url = winner.source_url
            else:
                summary.missed += 1

            summary.resolutions.append(resolution)

            if dry_run:
                continue

            # Apply: write the winner + provenance, or just stamp the attempt.
            now = datetime.now(tz=UTC)
            if winner is not None:
                company.website = winner.website
                company.website_source = winner.source
                company.website_source_url = winner.source_url
                company.website_resolved_at = now
            company.website_fallback_checked_at = now
            session.add(company)
            try:
                await session.commit()
            except StaleDataError:
                # Row merged out mid-run by a concurrent dedup — roll back, skip,
                # and fully revert this company's bookkeeping (hit or miss).
                await session.rollback()
                logger.warning(
                    "Company %s disappeared mid-resolve (concurrent merge) — skipping.",
                    company.id,
                )
                summary.errors += 1
                summary.resolutions.pop()
                if winner is not None:
                    summary.resolved -= 1
                else:
                    summary.missed -= 1
                if resolution.conflict:
                    summary.conflicts -= 1

    summary.per_source = [stats[s] for s in sources]
    logger.info(
        "resolve-website-fallback: seen=%d resolved=%d missed=%d errors=%d "
        "conflicts=%d dry_run=%s",
        summary.companies_seen,
        summary.resolved,
        summary.missed,
        summary.errors,
        summary.conflicts,
        dry_run,
    )
    return summary


def render_yield_table(summary: ResolveWebsiteFallbackSummary) -> str:
    """Render the dry-run yield table as GitHub-flavored markdown.

    Per-source hit rate, overall % resolved, conflict count (the wrong-site
    proxy), and a per-company detail table for manual review.
    """
    seen = summary.companies_seen
    pct = (summary.resolved / seen * 100) if seen else 0.0
    lines: list[str] = []
    lines.append("## resolve-website-fallback — dry-run yield")
    lines.append("")
    lines.append(f"- **Companies processed:** {seen}")
    lines.append(f"- **Resolved (≥1 accepted source):** {summary.resolved} ({pct:.0f}%)")
    lines.append(f"- **Missed:** {summary.missed}")
    lines.append(f"- **Cross-source conflicts:** {summary.conflicts} (wrong-site proxy)")
    lines.append(f"- **Errors:** {summary.errors}")
    lines.append("- **Cost:** $0 (Wikidata + news re-fetch are free)")
    lines.append("")
    lines.append("| Source | Attempted | Candidate | Accepted | Hit rate |")
    lines.append("|---|---:|---:|---:|---:|")
    for st in summary.per_source:
        hit = (st.accepted / st.attempted * 100) if st.attempted else 0.0
        lines.append(
            f"| {st.source} | {st.attempted} | {st.candidate_found} "
            f"| {st.accepted} | {hit:.0f}% |"
        )
    lines.append("")
    lines.append("| Company | Resolved website | Source | Conflict |")
    lines.append("|---|---|---|:---:|")
    for r in summary.resolutions:
        site = r.resolved_website or "—"
        src = r.source or "—"
        flag = "⚠️" if r.conflict else ""
        lines.append(f"| {r.name} | {site} | {src} | {flag} |")
    return "\n".join(lines)
