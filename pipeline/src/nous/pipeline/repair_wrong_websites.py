"""repair-wrong-websites pipeline stage — idempotent poisoned-row repair.

Six passes (spec 2026-06-13 Task 2.2; pass (e) added 2026-06-16; pass (f)
added 2026-07-17 — clears the people/competitors/industry/HQ/embedding residue
that pre-fix resets left behind on already-reset rows):

(a) Aggregator/directory URL: company.website host is in the shared
    AGGREGATOR_HOSTS reject set (or matches DIRECTORY_PATH_RE).  These were
    resolved by the old homepage resolver before the aggregator guard was added.

(b) For-sale / parked description: company.description_short contains
    domain-sale prose.  Deliberately a small in-Python regex against the stored
    description rather than re-parsing HTML (the HTML is gone by now; only the
    extracted text is stored).  Conservative: only triggers on explicit
    "for sale" / "buy this domain" language plus a domain word — avoids
    e-commerce false positives like SellRaze.

(c) False exclusion re-queue: rows with exclusion_reason IN ('not_a_startup',
    'non_us') whose exclusion_detail references "personal homepage" or
    "wrong site" / "wrong domain" — these were mis-excluded when the old
    resolver pointed them at an unrelated business.  Clearing the exclusion +
    eligibility timestamps lets judge-eligibility re-judge from a correct site.

(d) For-sale / parked PAGE content: the LLM sometimes narrates a for-sale
    lander as a real company, so the description escapes pass (b) (Foodology:
    "...a culinary content platform ... based on a site that is currently for
    sale", while the scraped page begins "foodology.com is for sale").  Pass (d)
    re-judges the stored RawPage.content — the extracted text IS still on record
    — with nous.sources.parked.page_is_for_sale_lander (a STRICTER detector than
    the resolver's, since scanning a real company's full page text false-positives
    on the looser signals), and resets live (non-excluded) rows as (a)/(b) do.

(e) Wrong-company profile: the stored profile is clearly about a DIFFERENT
    company than the row's name.  In production the hardened resolver still let a
    few of these through before it shipped: Kalshi (a prediction market) carried
    FrenFlow's description ("multi-venue prediction-market platform ... copy-trade
    across Polymarket, Kalshi, Predict.fun, Hyperliquid") because the resolver
    landed on FrenFlow's site, which merely lists Kalshi as a venue; AgentMail
    carried a "Series V" description.  Pass (e) is HIGH-PRECISION by double
    confirmation — it acts only when BOTH:
      1. description_short OPENS by naming a different company — a leading
         "<Other> is/provides/offers ..." whose subject does not fuzzy-match
         company.name (nous.util.title_subject.description_subject_mismatches),
         AND
      2. the stored homepage page's title line (the first line of the scraped
         RawPage.content, which scrape-homepages prepends from <title>) is NOT
         dominated by company.name (name_is_dominant_subject is False) — i.e. the
         page itself reads as a different brand, corroborating the description.
    A correctly-matched company ("Ramp is an all-in-one spend management
    platform ...") fails (1) — its subject IS the company — so it is never
    flagged.  Only live (non-excluded) rows are reset.

Repair action for (a)/(b)/(d)/(e):
    - Append bad URL to rejected_urls (so the hardened resolver never re-picks it)
    - Clear: website, website_resolved_at, description_short, description_long,
      primary_category, tags, last_enriched_at, last_enriched_payload,
      eligibility_checked_at, last_scrape_attempt_at
    - Drop raw_pages rows (stale content from the wrong site)
    - WRONG-COMPANY resets only (pass (e), or pass (a) with a confirmed
      description-subject mismatch): delete funding rounds + news articles
      SOURCED FROM the wrong site itself (primary_news_url / article url on
      the same host as the cleared website) — a news/aggregator "homepage"
      gets mined by the website-funding gap-fill and ingested as coverage,
      attributing OTHER companies' rounds to this row (2026-07-16 QA: helix
      carried Kinoa/Coval/ChatSee rounds from machinebrief.com). Same-host
      only, and NEVER on a bare aggregator-URL reset — AGGREGATOR_HOSTS
      includes real news publishers (techcrunch/reuters), which are invalid
      as homepages but the legitimate source of most rounds.

Repair action for (c):
    - Clear: exclusion_reason, exclusion_detail, excluded_at,
      eligibility_checked_at
    (website + descriptions stay — the new resolver may have already fixed the
    URL, or the next resolve-homepages run will.)

Idempotency:
    - (a): after repair, website IS NULL → no longer selected
    - (b): after repair, description_short IS NULL → no longer selected
    - (c): after repair, exclusion_reason IS NULL → no longer selected
    - (d): after repair, website IS NULL + raw_pages dropped → no longer selected
    - (e): after repair, website + description_short NULL + raw_pages dropped →
      no longer selected

``--dry-run`` logs intended actions without writing.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    Competitor,
    FundingRound,
    NewsArticle,
    Person,
    RawPage,
)
from nous.db.upsert import refresh_funding_round_count
from nous.sources.parked import page_is_for_sale_lander
from nous.sources.reject_hosts import is_aggregator_url, is_article_url
from nous.util.title_subject import (
    description_subject_mismatches,
    name_is_dominant_subject,
)
from nous.util.url import canonical_domain, hostname

logger = logging.getLogger(__name__)

# ── (b) description-text patterns ──────────────────────────────────────────
# Match stored description_short text that contains domain-sale prose.
# Only triggers when *both* a domain-sale phrase AND an e-commerce-distinguishing
# word ("domain" or "parked") appear, or on phrases that are unambiguously
# domain-sale without needing a co-occurring word (e.g. "buy this domain").
#
# SQL ILIKE patterns for the initial DB filter (broad net; Python regex narrows).
_PARKED_DESC_SQL_PATTERNS: tuple[str, ...] = (
    # Explicit domain-sale phrases that can't appear in real company copy
    "%domain%for sale%",
    "%for sale%domain%",
    "%buy this domain%",
    "%purchase this domain%",
    "%parked%domain%",
    "%domain%parked%",
    "%domain marketplace%",
    # "this site/website is for sale" on custom landers (Foodology-style)
    "%this site is for sale%",
    "%this website is for sale%",
    "%available for purchase%",
    "%inquire about this domain%",
)

# Python-side confirmation regex: must match the SQL-filtered candidate.
# Uses word boundaries to avoid false positives on substrings.
_PARKED_DESC_RE: re.Pattern[str] = re.compile(
    r"""
    (?:
        domain\s+(?:is\s+)?(?:for\s+sale|may\s+be\s+for\s+sale|marketplace|parking|parked)
        | (?:for\s+sale|buy|purchase)\s+(?:this\s+)?domain
        | parked\s+domain | domain\s+parked
        | (?:this\s+)?(?:site|website)\s+is\s+for\s+sale
        | available\s+for\s+purchase
        | inquire\s+about\s+this\s+domain
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ── (d) page-content tokens ────────────────────────────────────────────────
# Pass (b) keys on the LLM description, which the model sometimes writes with no
# domain-sale wording at all even when the page is a lander (Foodology: "...a
# culinary content platform ... based on a site that is currently for sale" —
# "site that is" misses pass (b)'s "site is for sale" regex). Pass (d) re-judges
# the stored RawPage.content (ground truth) with the STRICT backfill detector
# page_is_for_sale_lander. These SQL ILIKE tokens are a coarse pre-filter — every
# page that could trip that detector contains one, so over-selection is harmless
# (Python re-confirms), but the set MUST stay a superset of its triggers (the
# <host>-for-sale regex + the self-referential lander phrases). NOTE: this is the
# strict detector's trigger set, NOT the resolver's looser one — "available for
# purchase" / bare marketplace-brand intent are deliberately absent (they
# false-positive on real pages, e.g. At-Bay's "available for purchase").
_PAGE_CONTENT_SQL_TOKENS: tuple[str, ...] = (
    "for sale",
    "buy this domain",
    "purchase this domain",
    "inquire about this domain",
    "parked",
    "domain parking",
)

# ── (c) false-exclusion detail patterns ────────────────────────────────────
# Match exclusion_detail text that references the known mis-exclusion reasons:
# "personal homepage" or a wrong-site phrase.
_FALSE_EXCL_SQL_PATTERNS: tuple[str, ...] = (
    "%personal homepage%",
    "%personal home page%",
    "%wrong site%",
    "%wrong domain%",
    "%wrong website%",
    "%unrelated%site%",
    "%unrelated%domain%",
)

_FALSE_EXCL_REASONS: tuple[str, ...] = ("not_a_startup", "non_us")


class RepairWrongWebsitesSummary(BaseModel):
    aggregator_url_reset: int = 0
    parked_desc_reset: int = 0
    page_content_reset: int = 0
    false_exclusion_requeued: int = 0
    wrong_company_reset: int = 0
    # Rounds/articles sourced from the wrong site itself (same host as the
    # cleared website), deleted alongside the reset — see _reset_website_fields.
    wrong_site_rounds_deleted: int = 0
    wrong_site_articles_deleted: int = 0
    # Pass (f): already-reset rows still carrying poisoned enrichment residue
    # (people / competitors / industry / HQ from before the reset cleared them).
    enrichment_residue_cleared: int = 0
    dry_run: bool = False


async def run_repair_wrong_websites(
    session: AsyncSession, *, dry_run: bool = False
) -> RepairWrongWebsitesSummary:
    """Identify and repair poisoned rows.  Idempotent — a second run is a no-op."""
    summary = RepairWrongWebsitesSummary(dry_run=dry_run)
    now = datetime.now(tz=UTC)

    # ── Pass (a): aggregator / directory URL ─────────────────────────────────
    # Select companies that still have a website (the idempotency guard: after
    # reset, website IS NULL and they're never re-selected).
    aggregator_candidates = (
        (
            await session.execute(
                select(Company).where(Company.website.is_not(None))
            )
        )
        .scalars()
        .all()
    )

    for company in aggregator_candidates:
        if not company.website:
            continue
        # Aggregator/directory host, or a dated-article path on any host —
        # either way the stored "website" is a news/listing URL, not a
        # homepage (the blue-origin nypost.com case).
        if not (
            is_aggregator_url(company.website) or is_article_url(company.website)
        ):
            continue

        logger.info(
            "repair-wrong-websites (a): resetting aggregator URL %r for %r",
            company.website,
            company.name,
        )
        summary.aggregator_url_reset += 1
        if not dry_run:
            # Purge same-host rounds/articles only on the SAME double
            # confirmation pass (e) requires: the stored profile names a
            # DIFFERENT company AND the scraped page's title corroborates
            # (not dominated by this company's name) — the helix/machinebrief
            # class. Deletion is destructive, so a single fuzzy description
            # mismatch is not enough; a mere aggregator-URL website (e.g.
            # techcrunch.com) must keep its legitimately news-sourced rounds.
            purge = False
            if company.description_short and description_subject_mismatches(
                company.description_short, company.name
            ):
                page = await _homepage_page(session, company)
                title_line = _title_line(page.content) if page else ""
                purge = bool(title_line) and not name_is_dominant_subject(
                    title_line, company.name
                )
            rounds_gone, articles_gone = await _reset_website_fields(
                session, company, now, purge_wrong_site=purge
            )
            summary.wrong_site_rounds_deleted += rounds_gone
            summary.wrong_site_articles_deleted += articles_gone

    if not dry_run:
        await session.commit()

    # ── Pass (b): for-sale / parked description ──────────────────────────────
    parked_candidates = (
        (
            await session.execute(
                select(Company).where(
                    Company.description_short.is_not(None),
                    or_(
                        *[
                            Company.description_short.ilike(p)
                            for p in _PARKED_DESC_SQL_PATTERNS
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    for company in parked_candidates:
        if not company.description_short:
            continue
        if not _PARKED_DESC_RE.search(company.description_short):
            # SQL ILIKE was too broad; Python regex doesn't confirm — skip.
            continue

        logger.info(
            "repair-wrong-websites (b): resetting parked desc for %r (website %s; desc %r)",
            company.name,
            company.website,
            company.description_short[:80],
        )
        summary.parked_desc_reset += 1
        if not dry_run:
            rounds_gone, articles_gone = await _reset_website_fields(
                session, company, now
            )
            summary.wrong_site_rounds_deleted += rounds_gone
            summary.wrong_site_articles_deleted += articles_gone

    if not dry_run:
        await session.commit()

    # ── Pass (c): false exclusions ───────────────────────────────────────────
    false_excl_candidates = (
        (
            await session.execute(
                select(Company).where(
                    Company.exclusion_reason.in_(_FALSE_EXCL_REASONS),
                    Company.exclusion_detail.is_not(None),
                    or_(
                        *[
                            Company.exclusion_detail.ilike(p)
                            for p in _FALSE_EXCL_SQL_PATTERNS
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    for company in false_excl_candidates:
        logger.info(
            "repair-wrong-websites (c): re-queuing false exclusion %r "
            "(reason=%s, detail=%r)",
            company.name,
            company.exclusion_reason,
            (company.exclusion_detail or "")[:80],
        )
        summary.false_exclusion_requeued += 1
        if not dry_run:
            company.exclusion_reason = None
            company.exclusion_detail = None
            company.excluded_at = None
            # Clear the eligibility timestamp so judge-eligibility re-judges it.
            company.eligibility_checked_at = None
            session.add(company)

    if not dry_run:
        await session.commit()

    # ── Pass (d): for-sale / parked PAGE content ─────────────────────────────
    # The scraped page is ground truth. Only live rows (no exclusion) are reset,
    # so they re-enter resolve→scrape→enrich with the hardened resolver; excluded
    # for-sale rows are already hidden and left to pass (c) / judge-eligibility.
    page_candidates = (
        (
            await session.execute(
                select(Company)
                .where(Company.website.is_not(None))
                .where(Company.exclusion_reason.is_(None))
                .where(
                    exists().where(
                        RawPage.company_id == Company.id,
                        or_(
                            *[
                                RawPage.content.ilike(f"%{token}%")
                                for token in _PAGE_CONTENT_SQL_TOKENS
                            ]
                        ),
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    for company in page_candidates:
        page = await _homepage_page(session, company)
        if page is None or not page_is_for_sale_lander(page.content):
            # SQL token pre-filter was too broad; the strict detector doesn't
            # confirm the company's own homepage is a lander — skip.
            continue
        logger.info(
            "repair-wrong-websites (d): resetting for-sale lander %r "
            "(website %s; page %s)",
            company.name,
            company.website,
            page.url,
        )
        summary.page_content_reset += 1
        if not dry_run:
            rounds_gone, articles_gone = await _reset_website_fields(
                session, company, now
            )
            summary.wrong_site_rounds_deleted += rounds_gone
            summary.wrong_site_articles_deleted += articles_gone

    if not dry_run:
        await session.commit()

    # ── Pass (e): wrong-company profile ──────────────────────────────────────
    # HIGH-PRECISION: double-confirmed wrong-company match (description names a
    # different company AND the stored page title is a different brand). The SQL
    # net is broad on purpose (every live enriched row with a description); the
    # two Python confirmations below — both keyed on the conservative
    # title_subject helpers — are what make this safe to MUTATE.
    wrong_company_candidates = (
        (
            await session.execute(
                select(Company)
                .where(Company.website.is_not(None))
                .where(Company.exclusion_reason.is_(None))
                .where(Company.description_short.is_not(None))
            )
        )
        .scalars()
        .all()
    )

    for company in wrong_company_candidates:
        description = company.description_short
        if not description:
            continue
        # (1) Description must OPEN by naming a company that is clearly not this
        # row. description_subject_mismatches returns False unless it actually
        # extracted a named subject that fails the fuzzy-name match — so a
        # correctly-matched "Ramp is ..." (subject == company) is never selected.
        if not description_subject_mismatches(description, company.name):
            continue
        # (2) Corroborate with the stored page: the homepage title line must NOT
        # be dominated by the company name. scrape-homepages stores extracted
        # text with the <title> prepended as the first line, so the first
        # non-empty line is our title proxy. Requiring the page itself to read as
        # a different brand guards against a one-off odd description opener on a
        # row whose site is genuinely the company's.
        page = await _homepage_page(session, company)
        if page is None:
            continue
        title_line = _title_line(page.content)
        if not title_line or name_is_dominant_subject(title_line, company.name):
            continue

        logger.info(
            "repair-wrong-websites (e): resetting wrong-company profile %r "
            "(website %s; title %r; desc %r)",
            company.name,
            company.website,
            title_line[:80],
            description[:80],
        )
        summary.wrong_company_reset += 1
        if not dry_run:
            rounds_gone, articles_gone = await _reset_website_fields(
                session, company, now, purge_wrong_site=True
            )
            summary.wrong_site_rounds_deleted += rounds_gone
            summary.wrong_site_articles_deleted += articles_gone

    # ── Pass (f): enrichment residue on already-reset rows ───────────────────
    # Before 2026-07-17, _reset_website_fields cleared the website +
    # descriptions but left people / competitors / industry / HQ / embedding —
    # so helix kept machinebrief's leadership + media-entertainment industry
    # (and its real $10B round polluted /trends under the wrong industry).
    # Selection = the post-reset signature (website, description, AND
    # last_enriched_at all NULL — only the reset produces that combination)
    # plus at least one residue field/row still set. After the clear, nothing
    # matches → idempotent. New resets clear residue inline, so this pass
    # drains the pre-fix backlog then no-ops forever.
    residue_candidates = (
        (
            await session.execute(
                select(Company).where(
                    Company.website.is_(None),
                    Company.description_short.is_(None),
                    Company.last_enriched_at.is_(None),
                    or_(
                        Company.industry_group.is_not(None),
                        Company.hq_city.is_not(None),
                        Company.hq_state.is_not(None),
                        Company.hq_country.is_not(None),
                        exists().where(Person.company_id == Company.id),
                        exists().where(Competitor.company_id == Company.id),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for company in residue_candidates:
        logger.info(
            "repair-wrong-websites (f): clearing enrichment residue on %r "
            "(industry=%r hq=%r/%r)",
            company.name,
            company.industry_group,
            company.hq_city,
            company.hq_state,
        )
        summary.enrichment_residue_cleared += 1
        if not dry_run:
            await _clear_enrichment_residue(session, company)

    if not dry_run:
        await session.commit()

    return summary


def _title_line(content: str) -> str:
    """Return the first non-empty line of stored page *content* — the title proxy.

    scrape-homepages stores ``extract_visible_text`` output, which prepends the
    page ``<title>`` (and SEO meta) as the first section, so the first non-empty
    line is the page's title for our purposes.  Returns "" when *content* is
    blank.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


async def _homepage_page(session: AsyncSession, company: Company) -> RawPage | None:
    """Return the scraped page for the company's own homepage host, else newest.

    A company can have several raw_pages (the scraper fetches /, /about, ...).
    Judge only the page served from the resolved website's host so a for-sale
    phrase on a linked sub-resource cannot reset a real company; fall back to the
    most recent page when the host cannot be matched (e.g. a shared-hosting
    website, where ``canonical_domain`` returns None by design).
    """
    pages = (
        (
            await session.execute(
                select(RawPage)
                .where(RawPage.company_id == company.id)
                .order_by(RawPage.fetched_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not pages:
        return None
    site_domain = canonical_domain(company.website)
    if site_domain is not None:
        for page in pages:
            if canonical_domain(page.url) == site_domain:
                return page
    return pages[0]


async def _reset_website_fields(
    session: AsyncSession,
    company: Company,
    now: datetime,
    *,
    purge_wrong_site: bool = False,
) -> tuple[int, int]:
    """Clear all website + enrichment fields and drop stale raw_pages.

    The bad URL is appended to rejected_urls so the hardened resolver never
    re-picks it.  All enrichment timestamps are cleared so resolve→scrape→enrich
    run again cleanly.

    Also deletes funding rounds and news articles sourced from the WRONG site
    itself (same host as the cleared website): with a news/aggregator site as
    the "homepage", the website-funding gap-fill mines its pages and ingest
    attributes its syndicated posts — producing OTHER companies' rounds on this
    row (2026-07-16 QA: helix carried Kinoa/Coval/ChatSee rounds mined from
    machinebrief.com). Same-host-only is deliberate: rounds citing real
    third-party publishers are left alone (a cross-host misattribution is the
    news-attribution arc's job), and what remains is logged for audit. Returns
    ``(rounds_deleted, articles_deleted)``.
    """
    rounds_deleted = 0
    articles_deleted = 0
    bad_host = hostname(company.website) if company.website else ""
    # purge_wrong_site gates the deletion on WRONG-COMPANY evidence (pass (e),
    # or pass (a) with a confirmed description-subject mismatch). It must NOT
    # fire for a bare aggregator-URL reset: AGGREGATOR_HOSTS includes real news
    # publishers (techcrunch/reuters/bloomberg) that are invalid as HOMEPAGES
    # but are the legitimate source of most rounds — same-host deletion there
    # would destroy correct news-sourced rounds.
    if purge_wrong_site and bad_host:
        round_rows = (
            await session.execute(
                select(FundingRound.id, FundingRound.primary_news_url).where(
                    FundingRound.company_id == company.id,
                    FundingRound.primary_news_url.is_not(None),
                )
            )
        ).all()
        bad_round_ids = [
            rid for rid, url in round_rows if url and hostname(url) == bad_host
        ]
        if bad_round_ids:
            await session.execute(
                delete(FundingRound).where(FundingRound.id.in_(bad_round_ids))
            )
            rounds_deleted = len(bad_round_ids)
            await refresh_funding_round_count(session, company.id)
            logger.info(
                "repair-wrong-websites: deleted %d round(s) sourced from the "
                "wrong site %s for %r (%d round(s) with other sources kept)",
                rounds_deleted,
                bad_host,
                company.name,
                len(round_rows) - rounds_deleted,
            )
        article_rows = (
            await session.execute(
                select(NewsArticle.id, NewsArticle.url).where(
                    NewsArticle.company_id == company.id
                )
            )
        ).all()
        bad_article_ids = [
            aid for aid, url in article_rows if url and hostname(url) == bad_host
        ]
        if bad_article_ids:
            await session.execute(
                delete(NewsArticle).where(NewsArticle.id.in_(bad_article_ids))
            )
            articles_deleted = len(bad_article_ids)

    if company.website:
        existing: list[str] = list(company.rejected_urls or [])
        if company.website not in existing:
            company.rejected_urls = [*existing, company.website]

    company.website = None
    company.website_resolved_at = None
    company.description_short = None
    # Provenance goes with the description it described (0045) — a stale
    # 'fallback' tag on an empty description would mis-gate whatever is
    # written next.
    company.description_source = None
    company.description_long = None
    company.primary_category = None
    company.tags = None
    company.last_enriched_at = None
    company.last_enriched_payload = None
    company.eligibility_checked_at = None
    company.last_scrape_attempt_at = None

    # The rest of the poisoned enrichment must go too — helix kept
    # machinebrief's "Editorial Team" leadership, media-entertainment industry,
    # and AOL/BuzzFeed competitors long after its website + descriptions were
    # cleared, and the wrong industry_group filed a real $10B round under
    # media & entertainment on /trends (2026-07-17 verification).
    await _clear_enrichment_residue(session, company)

    # Drop stale raw_pages so scrape-homepages starts fresh.
    await session.execute(
        delete(RawPage).where(RawPage.company_id == company.id)
    )

    session.add(company)
    _ = now  # reserved for future audit-timestamp use
    return rounds_deleted, articles_deleted


async def _clear_enrichment_residue(
    session: AsyncSession, company: Company
) -> None:
    """Clear every enrichment-derived field/row a wrong-site profile poisons.

    Everything here is derived from the (wrong) scraped pages by the judge /
    describe / competitor prompts: leadership (people), competitors, the
    industry/HQ classification, and the embedding over the poisoned
    descriptions (which drives similar-companies until re-embedded).
    Employee-count fields stay — they are keyed on the company NAME against
    external sources, not the website. Deliberately does NOT touch funding
    rounds/articles (the same-host purge and the news-attribution arc own
    those) and does not commit — callers do.
    """
    company.industry_group = None
    company.hq_city = None
    company.hq_state = None
    company.hq_country = None
    company.hq_country_checked_at = None
    company.enrichment_prompt_version = None
    company.eligibility_prompt_version = None
    company.embedding = None
    company.embedded_at = None
    company.embedding_text_hash = None
    await session.execute(delete(Person).where(Person.company_id == company.id))
    await session.execute(
        delete(Competitor).where(Competitor.company_id == company.id)
    )
    session.add(company)
