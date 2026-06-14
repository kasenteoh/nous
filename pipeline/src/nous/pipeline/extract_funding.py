"""extract-funding pipeline stage.

For each unprocessed NewsArticle, call the LLM with the funding-extraction
prompt and persist the structured round/investor data. Marks the article
``processed=true`` either way (success, "not a funding announcement", or
low-confidence skip) so re-runs only revisit truly-unprocessed rows.

Also applies company status events (acquired / shut_down / ipo) reported by
the same extraction — see ``_apply_status_event``. This happens BEFORE the
is_funding_announcement gate, because exit articles are usually not funding
announcements.

Article-STATED cumulative totals ("has raised $285M to date") are applied the
same way — see ``_apply_total_raised`` — because they too appear in
non-funding coverage (acquisition articles recap funding history). They land
on the company row (total_raised_usd/_source_url/_as_of) with
newest-article-wins semantics; the web tile shows max(stated, sum-of-rounds).

Idempotency:
- ``processed`` flag is the work-queue gate; once set, the article is never
  re-extracted.
- ``reconcile_funding_round`` merges into existing rounds within the
  proximity window rather than inserting duplicates.
- ``upsert_investor`` is keyed on the canonicalized name.
- ``link_round_investor`` uses ON CONFLICT to merge `is_lead` (sticky-true).

Quota discipline (spec §11):
- Hard cap on articles processed per run (default 1000) to bound per-run
  LLM spend on DeepSeek.
- On LLMRateLimitError, stop the loop immediately — same pattern as M2's
  enrich-companies.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle, RawPage
from nous.db.upsert import (
    link_round_investor,
    reconcile_funding_round,
    upsert_investor,
)
from nous.llm.client import (
    MAX_PROMPT_INPUT_CHARS,
    LLMError,
    LLMParseError,
    LLMRateLimitError,
    complete_json,
)
from nous.llm.prompts.funding_extraction import (
    FundingExtraction,
    build_prompt,
    build_website_prompt,
)
from nous.pipeline.refresh_investor_counts import refresh_investor_counts
from nous.sources.reject_hosts import is_aggregator_url
from nous.util.text import extract_visible_text, truncate_to_chars

logger = logging.getLogger(__name__)

# Minimum cleaned website text length to bother extracting from.
_MIN_TEXT_CHARS = 200

# Image / media-hosting hosts whose URLs must never be accepted as a funding
# source.  These are not in AGGREGATOR_HOSTS (which covers directories and
# editorial sites), so we check them locally.
# TODO: fold these into reject_hosts.AGGREGATOR_HOSTS once the shared module
# is next edited.
_IMAGE_HOSTS: frozenset[str] = frozenset(
    {
        "imgur.com",
        "i.imgur.com",
        "i.redd.it",
        "preview.redd.it",
        "pbs.twimg.com",
        "cdn.discordapp.com",
        "media.giphy.com",
    }
)


def _is_junk_source_url(url: str) -> bool:
    """Return True when *url* is unsuitable as a funding-round source.

    Combines the shared aggregator/directory reject-list with a local check
    for obvious image/CDN hosts.  A company's own website is NOT junk — only
    third-party aggregator or image hosts are rejected.
    """
    if is_aggregator_url(url):
        return True
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if ":" in host:
        host = host.split(":")[0]
    bare = host[4:] if host.startswith("www.") else host
    return bare in _IMAGE_HOSTS or any(
        bare.endswith("." + h) for h in _IMAGE_HOSTS
    )

# Phrases that signal an article states a cumulative funding total. Used by
# the --requery-totals backfill to pick already-processed articles worth a
# second LLM pass; matched case-insensitively via ILIKE.
# Note: leading-wildcard ILIKE over news_articles.raw_content is deliberately
# unindexable — acceptable as a manual one-off lever bounded by --limit (the
# CLAUDE.md index-every-WHERE rule is for hot paths, not this backfill).
_TOTAL_PHRASES: tuple[str, ...] = (
    "%to date%",
    "%in total%",
    "%total funding%",
    "%total raised%",
    "%altogether%",
)


class _RoundPersistCounts(BaseModel):
    """Shared counters for code paths that reconcile a round + link investors."""

    funding_rounds_created: int = 0
    funding_rounds_merged: int = 0
    investors_created: int = 0
    investor_links_created: int = 0


def _apply_status_event(
    company: Company,
    extraction: FundingExtraction,
    *,
    source_url: str | None,
) -> Literal["changed", "backfilled"] | None:
    """Apply an acquired/shut_down/ipo event from *extraction* to *company*.

    Returns "changed" when ``status`` itself transitioned, "backfilled" when
    only a missing ``status_source_url`` was filled in, and None when the row
    is untouched. Any non-None outcome means the caller must ensure a commit.

    Rules:
    - Only acts on medium/high ``status_confidence`` — low is noise.
    - Never downgrades: a non-active status is never overwritten by the
      pipeline (manual correction is the escape hatch). A re-confirmation of
      the SAME status may backfill a missing ``status_source_url``.
    - ``status_source_url`` is always set together with ``status`` — every
      fact rendered on a company page needs a recorded source.

    Both mutations are logged at INFO: status flips are rare and high-impact,
    and the never-downgrade rule presumes a human can spot (and manually
    correct) a wrong one — the log line is what makes that review possible.
    """
    if extraction.status_event is None:
        return None
    if extraction.status_confidence not in ("medium", "high"):
        return None

    if company.status == "active":
        company.status = extraction.status_event
        company.status_source_url = source_url
        logger.info(
            "Company status applied: %r -> '%s' (source: %s)",
            company.name,
            extraction.status_event,
            source_url,
        )
        return "changed"

    if (
        company.status == extraction.status_event
        and company.status_source_url is None
        and source_url is not None
    ):
        company.status_source_url = source_url
        logger.info(
            "Company status source backfilled: %r stays '%s' (source: %s)",
            company.name,
            company.status,
            source_url,
        )
        return "backfilled"

    return None


def _apply_total_raised(
    company: Company,
    extraction: FundingExtraction,
    *,
    source_url: str | None,
    as_of: date,
) -> bool:
    """Apply an article-stated cumulative "total raised" to *company*.

    Returns True when the row was updated (the caller must ensure a commit),
    False when untouched.

    Rules:
    - Only acts when the extraction carries a stated total. The prompt forbids
      the model from summing or inferring one, so a non-null value is always
      an explicit claim in the source text — never fabricated here either.
    - Newest-article-wins: records when the company has no stated total yet,
      or when *as_of* is strictly newer than the recorded total_raised_as_of.
      A newer-but-SMALLER stated total still wins — it is the most recent
      source claim, and the web tile shows max(stated, sum-of-rounds), so an
      understated claim can never drag the display below the known rounds.
      Same-day or older claims never overwrite, keeping re-runs idempotent.
      An existing claim with a NULL as_of (manual edit) can't assert recency,
      so any dated claim supersedes it.
    - All three columns travel together: ``total_raised_source_url``
      satisfies the every-rendered-fact-needs-a-source rule and
      ``total_raised_as_of`` orders future claims.

    Applied totals are logged at INFO, like status events: newest-wins
    presumes a human can spot (and manually correct) a bad claim, and the
    log line is what makes that review possible.
    """
    if extraction.total_raised_usd is None:
        return False

    if (
        company.total_raised_usd is not None
        and company.total_raised_as_of is not None
        and as_of <= company.total_raised_as_of
    ):
        return False

    company.total_raised_usd = extraction.total_raised_usd
    company.total_raised_source_url = source_url
    company.total_raised_as_of = as_of
    logger.info(
        "Company total raised recorded: %r -> %s USD as of %s (source: %s)",
        company.name,
        extraction.total_raised_usd,
        as_of,
        source_url,
    )
    return True


async def _persist_round_and_investors(
    session: AsyncSession,
    counts: _RoundPersistCounts,
    *,
    company_id: UUID,
    extraction: FundingExtraction,
    primary_news_url: str,
    proximity_days: int,
) -> None:
    """Reconcile a funding round and link its investors, updating *counts*.

    Shared by the news (primary) and website-fallback paths. Reconciliation is
    fill-nulls + first-write-wins on primary_news_url, so running the website
    path after the news path can only fill gaps TechCrunch left.
    """
    funding_round, created = await reconcile_funding_round(
        session,
        company_id=company_id,
        extraction=extraction,
        primary_news_url=primary_news_url,
        proximity_days=proximity_days,
    )
    if created:
        counts.funding_rounds_created += 1
    else:
        counts.funding_rounds_merged += 1

    for is_lead, names in (
        (True, extraction.lead_investors),
        (False, extraction.other_investors),
    ):
        for investor_name in names:
            if not investor_name.strip():
                continue
            try:
                investor, inv_created = await upsert_investor(
                    session, name=investor_name
                )
            except ValueError:
                continue
            if inv_created:
                counts.investors_created += 1
            await link_round_investor(
                session,
                funding_round_id=funding_round.id,
                investor_id=investor.id,
                is_lead=is_lead,
            )
            counts.investor_links_created += 1


class ExtractFundingSummary(_RoundPersistCounts):
    articles_processed: int = 0
    llm_failures: int = 0
    skipped_not_funding: int = 0
    skipped_low_confidence: int = 0
    skipped_rate_limited: int = 0
    # Companies whose `status` value actually transitioned (active -> exit).
    status_changes_applied: int = 0
    # Same-status re-confirmations that only filled a NULL status_source_url.
    status_sources_backfilled: int = 0
    # Companies whose stated cumulative total was recorded/superseded.
    totals_recorded: int = 0


async def run_extract_funding(
    session: AsyncSession,
    *,
    limit: int = 1000,
    skip_low_confidence: bool = True,
    proximity_days: int = 60,
    requery_totals: bool = False,
) -> ExtractFundingSummary:
    """Walk unprocessed news_articles oldest-first and extract funding rounds.

    With ``requery_totals=True`` the selection flips to a one-time backfill:
    articles ALREADY processed whose text matches a cumulative-total phrase
    (``_TOTAL_PHRASES``, case-insensitive) and whose company has no stated
    total yet — totals landed in the schema after those articles were
    consumed, so this is the lever that re-reads them. The articles re-run
    the exact same extraction+apply path, which is idempotent end to end:
    rounds reconcile into existing rows, status never downgrades, totals
    apply newest-wins, and the articles stay processed (the flag is never
    cleared, so the normal queue is unaffected).
    """
    summary = ExtractFundingSummary()

    if requery_totals:
        stmt = (
            select(NewsArticle)
            .join(Company, Company.id == NewsArticle.company_id)
            .where(NewsArticle.processed.is_(True))
            .where(
                or_(*(NewsArticle.raw_content.ilike(p) for p in _TOTAL_PHRASES))
            )
            .where(Company.total_raised_usd.is_(None))
        )
    else:
        stmt = select(NewsArticle).where(NewsArticle.processed.is_(False))
    stmt = stmt.order_by(
        NewsArticle.published_date.desc().nulls_last(),
        NewsArticle.created_at.asc(),
    ).limit(limit)
    result = await session.execute(stmt)
    articles = result.scalars().all()

    for article in articles:
        # Need the owning company's name for the prompt.
        company = await session.get(Company, article.company_id)
        if company is None:
            logger.warning(
                "news_article %s references missing company_id %s — marking processed",
                article.id,
                article.company_id,
            )
            article.processed = True
            session.add(article)
            await session.commit()
            continue

        prompt = build_prompt(
            company_name=company.name,
            article_text=article.raw_content,
        )

        try:
            extraction: FundingExtraction = await complete_json(prompt, FundingExtraction)
        except LLMRateLimitError:
            logger.warning(
                "LLM rate limit hit while extracting funding for %s — stopping"
                " loop to avoid further quota exhaustion.",
                company.name,
            )
            summary.skipped_rate_limited += 1
            break
        except (LLMParseError, LLMError) as exc:
            logger.warning(
                "LLM error extracting funding from %s: %s", article.url, exc
            )
            summary.llm_failures += 1
            # Leave processed=false so a future run with a fixed prompt can retry.
            continue

        summary.articles_processed += 1

        # Status events (acquired / shut_down / ipo) apply BEFORE the
        # is_funding_announcement gate: acquisition/shutdown articles are
        # usually NOT funding announcements, and catching them is the whole
        # point of the field.
        status_outcome = _apply_status_event(
            company, extraction, source_url=article.url
        )
        if status_outcome is not None:
            if status_outcome == "changed":
                summary.status_changes_applied += 1
            else:
                summary.status_sources_backfilled += 1
            session.add(company)

        # Stated cumulative totals also apply BEFORE the gate: they appear in
        # non-funding coverage too (e.g. acquisition articles recapping the
        # company's funding history). as_of falls back to today when the feed
        # gave no published date, so the claim still participates in the
        # newest-wins ordering.
        total_recorded = _apply_total_raised(
            company,
            extraction,
            source_url=article.url,
            as_of=article.published_date or datetime.now(tz=UTC).date(),
        )
        if total_recorded:
            summary.totals_recorded += 1
            session.add(company)

        if not extraction.is_funding_announcement:
            summary.skipped_not_funding += 1
            article.processed = True
            session.add(article)
            await session.commit()
            continue

        if skip_low_confidence and extraction.confidence == "low":
            summary.skipped_low_confidence += 1
            # Leave processed=False so a future run with a tightened prompt
            # (or `--include-low-confidence`) can retry. "Not a funding
            # announcement" is terminal-skip above; "low confidence" is a
            # transient-skip pending better extraction.
            if status_outcome is not None or total_recorded:
                # The ROUND extraction is deferred, but the status event /
                # stated total carry their own validity — persist them now
                # rather than leaving them pending on a path that never
                # commits.
                await session.commit()
            continue

        await _persist_round_and_investors(
            session,
            summary,
            company_id=company.id,
            extraction=extraction,
            primary_news_url=article.url,
            proximity_days=proximity_days,
        )

        article.processed = True
        session.add(article)
        await session.commit()

    # Recompute portfolio_count for all investors now that funding-round
    # investor links may have been added. Committed in its own transaction so a
    # count failure doesn't roll back the extraction work.
    await refresh_investor_counts(session)
    await session.commit()

    return summary


class ExtractFundingWebsiteSummary(_RoundPersistCounts):
    companies_seen: int = 0
    companies_with_funding: int = 0
    llm_failures: int = 0
    skipped_no_text: int = 0
    skipped_not_funding: int = 0
    skipped_low_confidence: int = 0
    skipped_rate_limited: int = 0
    # Companies skipped because their website URL is a junk/image/aggregator host.
    skipped_junk_source: int = 0
    # Companies whose `status` value actually transitioned (active -> exit).
    status_changes_applied: int = 0
    # Same-status re-confirmations that only filled a NULL status_source_url.
    status_sources_backfilled: int = 0
    # Companies whose stated cumulative total was recorded/superseded.
    totals_recorded: int = 0


async def run_extract_funding_website(
    session: AsyncSession,
    *,
    limit: int | None = None,
    skip_low_confidence: bool = True,
    proximity_days: int = 60,
    recheck_after_days: int = 180,
) -> ExtractFundingWebsiteSummary:
    """Gap-fill funding from a company's own scraped website.

    Fallback to the news/TechCrunch path: runs ONLY for companies that have
    raw_pages but no funding_rounds yet, so TechCrunch always remains the
    primary source. Idempotent — reconcile_funding_round dedups/fills-nulls, so
    re-running (e.g. after a company gains a TechCrunch round) won't clobber it.

    Every attempt stamps ``website_funding_checked_at`` (including "site says
    nothing about funding", which is the common case) and the selection takes
    least-recently-checked first with a ``recheck_after_days`` back-off.
    Without that, no-funding companies stay eligible forever and a bounded
    daily run re-LLM's the same head of the list every day. The long default
    back-off is deliberate: marketing sites rarely gain funding pages, and the
    news path remains the primary detector for new rounds.
    """
    summary = ExtractFundingWebsiteSummary()

    recheck_cutoff = datetime.now(tz=UTC) - timedelta(days=recheck_after_days)

    stmt = (
        select(Company)
        .where(exists().where(RawPage.company_id == Company.id))
        .where(~exists().where(FundingRound.company_id == Company.id))
        .where(
            or_(
                Company.website_funding_checked_at.is_(None),
                Company.website_funding_checked_at < recheck_cutoff,
            )
        )
        .where(Company.exclusion_reason.is_(None))
        .order_by(
            Company.website_funding_checked_at.asc().nulls_first(),
            Company.name.asc(),
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    companies = result.scalars().all()

    for company in companies:
        summary.companies_seen += 1

        pages_result = await session.execute(
            select(RawPage)
            .where(RawPage.company_id == company.id)
            .order_by(RawPage.url.asc())
        )
        pages = pages_result.scalars().all()

        parts = [extract_visible_text(page.content) for page in pages]
        combined = "\n\n".join(p for p in parts if p)
        cleaned = truncate_to_chars(combined, MAX_PROMPT_INPUT_CHARS)

        if len(cleaned) >= _MIN_TEXT_CHARS:
            prompt = build_website_prompt(company_name=company.name, page_text=cleaned)

            try:
                extraction: FundingExtraction = await complete_json(
                    prompt, FundingExtraction
                )
            except LLMRateLimitError:
                logger.warning(
                    "LLM rate limit hit while extracting website funding for %s —"
                    " stopping loop to avoid further quota exhaustion.",
                    company.name,
                )
                summary.skipped_rate_limited += 1
                # Deliberately NOT stamped: the attempt never completed, and a
                # transient provider limit shouldn't cost the company its slot
                # for the whole recheck window.
                break
            except (LLMParseError, LLMError) as exc:
                logger.warning(
                    "LLM error extracting website funding for %s: %s",
                    company.name,
                    exc,
                )
                summary.llm_failures += 1
            else:
                # Own-site status notices ("we've been acquired by X", "we are
                # winding down") apply regardless of whether the page states
                # funding. The prompt caps status_confidence at 'medium',
                # which still passes the helper's medium/high gate. The
                # end-of-loop stamp commit persists the change.
                status_outcome = _apply_status_event(
                    company,
                    extraction,
                    source_url=company.website
                    or (pages[0].url if pages else None),
                )
                if status_outcome == "changed":
                    summary.status_changes_applied += 1
                elif status_outcome == "backfilled":
                    summary.status_sources_backfilled += 1

                # Own-site stated totals ("we've raised $X to date") apply
                # regardless of whether the page states a round. as_of is
                # today — a live page asserts its claim now, and there is no
                # publication date to prefer. The end-of-loop stamp commit
                # persists the change.
                if _apply_total_raised(
                    company,
                    extraction,
                    source_url=company.website
                    or (pages[0].url if pages else None),
                    as_of=datetime.now(tz=UTC).date(),
                ):
                    summary.totals_recorded += 1

                if not extraction.is_funding_announcement:
                    summary.skipped_not_funding += 1
                elif skip_low_confidence and extraction.confidence == "low":
                    summary.skipped_low_confidence += 1
                else:
                    # Attribute the round to the company's website (the source
                    # of the text).
                    source_url = company.website or (pages[0].url if pages else "")
                    if source_url and _is_junk_source_url(source_url):
                        # Reject rounds whose attributed URL is an image host,
                        # CDN, or aggregator directory.  A company's own domain
                        # is never rejected here — only third-party junk URLs.
                        logger.warning(
                            "Skipping website funding for %r: source URL is a "
                            "junk/aggregator host (%s)",
                            company.name,
                            source_url,
                        )
                        summary.skipped_junk_source += 1
                    else:
                        await _persist_round_and_investors(
                            session,
                            summary,
                            company_id=company.id,
                            extraction=extraction,
                            primary_news_url=source_url,
                            proximity_days=proximity_days,
                        )
                        summary.companies_with_funding += 1
        else:
            summary.skipped_no_text += 1

        # Stamp the attempt on every completed path (funding found, nothing
        # found, thin text, or LLM failure) so the rotation advances. One
        # commit per company; this also commits the round persisted above.
        company.website_funding_checked_at = datetime.now(tz=UTC)
        session.add(company)
        await session.commit()

    return summary
