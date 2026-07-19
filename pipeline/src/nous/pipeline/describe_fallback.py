"""describe-fallback — third-party-grounded description_short (GENERATIVE, GATED).

The owner-approved "describe-fallback" (BACKLOG 2026-07-19 "missing-data
residue"; the deferred option "A" re-opened). Normal descriptions are written
ONLY from a company's own scraped pages; this stage targets the residue that has
NO readable own pages — companies nous shows but cannot describe because the
homepage is Cloudflare-403'd or absent. For each, it assembles third-party
EVIDENCE nous already holds — Wikidata entity facts plus entity-guard-
corroborated news coverage — and asks the ``describe_fallback`` prompt for a
SHORT factual description, gated hard: every clause traceable to the shown
evidence, null over thin, and a code-checked grounding descriptor.

Two modes:

- ``--dry-run`` (the #243 evidence gate, kept) runs the whole pipeline (cohort →
  evidence → LLM → post-validation) and reports a yield table so the owner can
  see what fraction of the residue gets a grounded description, at what
  confidence, and how often the model tries to describe on funding facts alone.
  It writes NOTHING — no ``description_short``, no provenance, no stamp.
- apply (``--apply``) additionally PERSISTS, per company: writes
  ``description_short`` + ``description_source='fallback'`` for a grounded,
  non-low-confidence, claim-checked description (re-checking ``description_short
  IS NULL`` right before the write so a concurrent enrich is never clobbered),
  and stamps ``companies.describe_fallback_prompt_version`` on every completed
  adjudication so a company that correctly yields no description is not
  re-billed. Selection is version-gated (stamp NULL OR < PROMPT_VERSION), so a
  prompt bump re-selects everyone while stamped rows never re-bill.

The moat rule (this is a GENERATIVE stage, so the gates are stricter than
anywhere else): the description is never trusted on the model's word. Three
code-level checks back the prompt's own rules — the prompt's validator drops a
description lacking a grounding descriptor; this stage verifies that the echoed
descriptor actually appears in the evidence text (an ungrounded echo is
discarded); and a token-level claim check (M1) requires most of the
description's own content words to appear in the evidence, so a fluent
paraphrase that drifts past the descriptor is caught too. Wrong-entity news is
filtered before it becomes evidence by the same cheap corroboration + entity
guard the ingest path uses. A dumb country-adjective regex additionally flags
non-US-looking descriptions for the ops exclusion flow (transparent, no LLM).
Cost: one DeepSeek call per company that has any evidence (skipped otherwise);
the exact spend lands in the ``emit_run_telemetry`` block.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field
from sqlalchemy import exists, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle, RawPage
from nous.llm.client import LLMError, LLMRateLimitError, complete_json
from nous.llm.prompts.describe_fallback import (
    MAX_EVIDENCE_CHARS,
    PROMPT_VERSION,
    DescribeFallbackResult,
    build_prompt,
)
from nous.pipeline.entity_guard import check_article_entity
from nous.sources.wikidata import WikidataClient, WikidataFacts
from nous.util.entity_corroboration import best_corroboration
from nous.util.text import truncate_to_chars
from nous.util.url import hostname

logger = logging.getLogger(__name__)

# A raw_page with < this much content is not a readable own-page — mirrors the
# enrich / career-history text floor. A company whose ONLY pages are thinner than
# this is still "unscrapable residue" and belongs in the cohort.
_MIN_RAW_PAGE_CHARS = 200

# How many of a company's most-recent articles to consider as evidence. Kept
# small: the identity-establishing coverage is almost always the freshest, and
# every survivor costs an entity-guard adjudication.
_MAX_ARTICLES_PER_COMPANY = 6

# Per-article excerpt length in the evidence block. Raised 400→800 for the
# optional LONG profile (2026-07-19.2): a grounded multi-paragraph profile
# needs more than the headline + lede a tagline lives on. The combined block is
# still capped by MAX_EVIDENCE_CHARS.
_ARTICLE_EXCERPT_CHARS = 800

# Safety cap on the per-company sample list when --limit is unbounded.
_MAX_SAMPLES = 50

# How much of an accepted LONG profile to echo into the yield-table sample —
# enough to eyeball its register/grounding without bloating the summary jsonb.
_SAMPLE_LONG_CHARS = 200


class CompanySample(BaseModel):
    """One company's probe outcome — the per-company review row."""

    slug: str
    evidence_sources: int = 0  # distinct citations fed to the LLM (wikidata + articles)
    wikidata: bool = False  # a Wikidata facts hit contributed evidence
    description_short: str | None = None
    description_long: str | None = None  # accepted LONG profile, truncated for the table
    grounding_descriptor: str | None = None
    confidence: str | None = None
    null_reason: str | None = None  # why no (persistable) description, when so


class DescribeFallbackSummary(BaseModel):
    """Stage summary — feeds the yield table, telemetry, and pipeline_runs."""

    dry_run: bool
    prompt_version: str
    cohort_selected: int = 0
    wikidata_hits: int = 0
    articles_seen: int = 0
    articles_corroborated: int = 0  # passed the cheap deterministic corroboration
    guard_dropped: int = 0  # dropped by corroboration-suspect or the LLM guard
    guard_errors: int = 0  # guard skipped an article on an LLM error
    guard_rate_limited: bool = False  # guard 429 → stopped adjudicating for the run
    skipped_no_evidence: int = 0  # no wikidata hit AND no surviving article → no call
    llm_calls: int = 0
    described: int = 0  # produced a grounded (descriptor-checked) description
    long_written: int = 0  # accepted an evidence-proportional LONG profile
    long_below_evidence_bar: int = 0  # long dropped: < 3 distinct evidence sources
    long_claims_not_grounded: int = 0  # M1: long content words absent from evidence
    null_no_descriptor: int = 0  # model returned null: no non-funding descriptor
    null_insufficient: int = 0  # model returned null: insufficient evidence
    null_ambiguity: int = 0  # model returned null: entity ambiguity
    descriptor_not_in_evidence: int = 0  # echoed descriptor absent from evidence
    claims_not_grounded: int = 0  # M1: description content words absent from evidence
    low_confidence: int = 0  # described but confidence=='low' (would NOT persist)
    errors: int = 0  # per-company LLM errors (non-rate-limit)
    # Apply-mode counters (0 in dry-run).
    persisted: int = 0  # description_short written (grounded, non-low, claim-checked)
    skipped_already_described: int = 0  # re-check found description_short already set
    stale_cleared: int = 0  # fallback rows cleared on a distrust-class null re-adjudication
    # Non-US side-finding: slugs whose produced description reads non-US (a dumb
    # country-adjective/city regex, no LLM) — persisted normally, surfaced for
    # the ops exclusion flow.
    non_us_suspects: list[str] = Field(default_factory=list)
    samples: list[CompanySample] = Field(default_factory=list)


# ── evidence assembly (pure) ────────────────────────────────────────────────


def _wikidata_lines(facts: WikidataFacts) -> list[str]:
    """Labeled evidence lines from Wikidata facts, each citing the entity page.

    Description first (it is the descriptor the prompt gates on), then the
    supporting facts. Funding is deliberately absent — Wikidata carries none, and
    the prompt forbids funding as a descriptor anyway.
    """
    src = facts.entity_url
    lines: list[str] = []
    if facts.entity_description:
        lines.append(f"Wikidata description: {facts.entity_description} (source: {src})")
    if facts.industries:
        lines.append(f"Industry: {', '.join(facts.industries)} (source: {src})")
    if facts.inception_year is not None:
        lines.append(f"Founded: {facts.inception_year} (source: {src})")
    if facts.hq:
        lines.append(f"Headquarters: {', '.join(facts.hq)} (source: {src})")
    if facts.founders:
        lines.append(f"Founders: {', '.join(facts.founders)} (source: {src})")
    return lines


def _article_block(article: NewsArticle) -> str:
    """A "TITLE (source: url)" line plus a short raw-content excerpt."""
    excerpt = truncate_to_chars(article.raw_content or "", _ARTICLE_EXCERPT_CHARS)
    block = f"{article.title} (source: {article.url})"
    if excerpt.strip():
        block = f"{block}\n{excerpt}"
    return block


def _assemble_evidence(
    wikidata_lines: list[str], article_blocks: list[str]
) -> str:
    """Wikidata facts first, then article title/excerpt blocks, truncated to the
    prompt's evidence budget."""
    parts = [*wikidata_lines, *article_blocks]
    return truncate_to_chars("\n\n".join(parts), MAX_EVIDENCE_CHARS)


# Descriptors that carry no identity signal on their own — an echo of one of
# these would pass the substring check vacuously (every Wikidata description
# says "company"). Review catch (M2).
_GENERIC_DESCRIPTORS: frozenset[str] = frozenset(
    {"company", "business", "startup", "organization", "organisation", "firm"}
)

# Provenance suffixes inside evidence lines ("(source: https://…)"). Stripped
# before the descriptor check so a phrase living only in a source URL can
# never license a description (review catch, M4).
_SOURCE_SUFFIX_RE = re.compile(r"\(source: [^)]*\)")


def _normalize_evidence(evidence: str) -> str:
    """URL-stripped, whitespace/case-normalized evidence — the shared basis for
    both the descriptor check and the M1 claim check.

    Source-URL suffixes ("(source: https://…)") are stripped first so a phrase
    appearing only inside a citation URL is never treated as editorial evidence
    (review catch M4).
    """
    stripped = _SOURCE_SUFFIX_RE.sub(" ", evidence)
    return " ".join(stripped.lower().split())


def _descriptor_in_evidence(descriptor: str | None, evidence: str) -> bool:
    """Does ``descriptor`` appear in ``evidence`` case-insensitively after
    whitespace normalization? The moat-critical post-validation: the model's
    echoed grounding descriptor is verified against the shown evidence, never
    trusted on its word (the same grounded-quote discipline as source_verification).

    Two review-hardened refinements: source-URL suffixes are stripped from the
    evidence first (a phrase appearing only in a URL is not editorial
    evidence), and trivially generic/short descriptors ("company", "AI") are
    rejected — they match everywhere while licensing nothing.
    """
    if descriptor is None or not descriptor.strip():
        return False
    norm_desc = " ".join(descriptor.lower().split())
    if len(norm_desc) < 5 or norm_desc in _GENERIC_DESCRIPTORS:
        return False
    return norm_desc in _normalize_evidence(evidence)


# ── M1: token-level claim check ──────────────────────────────────────────────

# Words that carry no identity signal — dropped before the M1 claim check so a
# description is scored on its CONTENT words, not connective tissue or the
# generic verbs every profile shares. Small and deliberately generic; the
# company's own name tokens are dropped too (a description naturally repeats its
# subject's name, which third-party evidence phrased about "the company" need
# not echo).
_CLAIM_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "builds", "based",
        "by", "company", "companies", "develops", "for", "from", "in", "into",
        "is", "it", "its", "makes", "of", "offers", "on", "or", "platform",
        "product", "products", "provider", "provides", "service", "services",
        "software", "solution", "solutions", "that", "the", "their", "they",
        "this", "to", "was", "were", "which", "with", "also", "using", "used",
    }
)

# Fraction of a description's content words that must appear in the evidence for
# the M1 claim check to pass. The descriptor check verifies only the ONE echoed
# phrase; this backs it with a floor over the WHOLE sentence, so a description
# that grounds its descriptor but invents the rest of the clause is still
# caught. 0.6 tolerates the connective/synonym slack an honest present-tense
# rewrite of third-party facts carries.
_CLAIM_GROUNDING_FLOOR = 0.6

_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _claim_is_grounded(description: str, company_name: str, evidence: str) -> bool:
    """M1: do >= ``_CLAIM_GROUNDING_FLOOR`` of the description's content words
    appear in the (URL-stripped, normalized) evidence?

    Content words are the description's alphanumeric tokens minus the stopword
    set and the company's own name tokens. Uses the SAME normalization as
    ``_descriptor_in_evidence`` (source URLs stripped, whitespace/case folded),
    substring-matched. Vacuously true when nothing remains to check (the
    descriptor gate has already run, so an all-stopword/name description is not
    re-litigated here).
    """
    norm_evidence = _normalize_evidence(evidence)
    name_tokens = set(_WORD_TOKEN_RE.findall(company_name.lower()))
    content = [
        tok
        for tok in _WORD_TOKEN_RE.findall(description.lower())
        if len(tok) > 1 and tok not in _CLAIM_STOPWORDS and tok not in name_tokens
    ]
    if not content:
        return True
    found = sum(1 for tok in content if tok in norm_evidence)
    return found / len(content) >= _CLAIM_GROUNDING_FLOOR


# ── non-US side-finding (dumb, transparent, no LLM) ──────────────────────────

# A produced description matching any of these reads non-US; it is persisted
# normally but its slug is surfaced under summary.non_us_suspects for the ops
# exclusion flow. Deliberately dumb — a word-boundary regex over common
# country adjectives and major non-US cities, no LLM, no geocoding.
_NON_US_RE = re.compile(
    r"\b(?:"
    r"indian|canadian|british|german|french|chinese|israeli|japanese|korean|"
    r"australian|dutch|swedish|swiss|spanish|italian|brazilian|mexican|"
    r"singaporean|irish|finnish|norwegian|danish|belgian|austrian|polish|"
    r"bengaluru|bangalore|london|berlin|munich|toronto|vancouver|montreal|"
    r"paris|tokyo|shanghai|beijing|shenzhen|singapore|sydney|melbourne|"
    r"amsterdam|stockholm|zurich|dublin|seoul|mumbai|delhi|"
    r"tel aviv|sao paulo|mexico city"
    r")\b",
    re.IGNORECASE,
)


def _looks_non_us(description: str) -> bool:
    """True when the description matches the dumb non-US country/city regex."""
    return _NON_US_RE.search(description) is not None


# ── news corroboration ──────────────────────────────────────────────────────


async def _surviving_articles(
    session: AsyncSession,
    company: Company,
    summary: DescribeFallbackSummary,
) -> list[NewsArticle]:
    """The company's recent articles that survive corroboration + the entity guard.

    Two layers, mirroring the ingest path: the cheap deterministic corroboration
    (:func:`best_corroboration`, $0) drops same-name different-entity shapes
    (lowercase-only / longer-entity-phrase) even without a profile; then the entity
    guard adjudicates the rest. The cohort has no ``description_short``, so the
    guard's no-profile fast path attaches most survivors — the cheap layer does the
    real wrong-entity filtering here. A guard 429 opens the circuit for the rest of
    the run (``allow_llm=False``): further articles that would need a call skip.
    """
    articles = (
        (
            await session.execute(
                select(NewsArticle)
                .where(NewsArticle.company_id == company.id)
                .order_by(
                    NewsArticle.published_date.desc().nulls_last(),
                    NewsArticle.created_at.desc(),
                )
                .limit(_MAX_ARTICLES_PER_COMPANY)
            )
        )
        .scalars()
        .all()
    )
    host = hostname(company.website) if company.website else ""
    own_context = f"{host} {company.slug}".strip()

    survivors: list[NewsArticle] = []
    for article in articles:
        summary.articles_seen += 1
        title = article.title or ""
        body = article.raw_content or ""
        # Same title-in-body dedup shape as the entity guard (review L1).
        combined = body if title.strip() in body else f"{title}. {body}"

        # Layer 1: cheap corroboration (profile is empty for this cohort).
        cheap = best_corroboration(
            company.name, None, combined, own_context=own_context or None
        )
        if not cheap.suspect:
            summary.articles_corroborated += 1
        else:
            # A wrong-entity shape the cheap layer is confident about — drop it
            # without spending an LLM call.
            summary.guard_dropped += 1
            logger.info(
                "describe-fallback: corroboration dropped article for %s: %s",
                company.slug,
                title[:90],
            )
            continue

        # Layer 2: the entity guard (belt-and-suspenders; mostly no-profile attach).
        decision = await check_article_entity(
            company,
            title=title,
            text=body,
            allow_llm=not summary.guard_rate_limited,
        )
        if decision.rate_limited:
            summary.guard_rate_limited = True
        if not decision.attach:
            if decision.llm_error:
                summary.guard_errors += 1
            else:
                summary.guard_dropped += 1
            continue
        survivors.append(article)
    return survivors


# ── stage ───────────────────────────────────────────────────────────────────


async def _persist_company(
    session: AsyncSession,
    company: Company,
    to_persist: str | None,
    long_to_persist: str | None,
    summary: DescribeFallbackSummary,
    *,
    clear_stale: bool = False,
) -> None:
    """Apply-mode write for one company: optional description + version stamp; commit.

    Stamps ``describe_fallback_prompt_version`` (the caller only reaches here
    for a COMPLETED adjudication or a clean skipped-no-evidence, both of which
    must not re-select next run). When ``to_persist`` is a description, the
    write is a single conditional ``UPDATE … WHERE (description_short IS NULL OR
    description_source = 'fallback')`` — atomic check-and-set. The WHERE is
    widened from the original ``description_short IS NULL`` so the stage may
    REFRESH its OWN stopgap on a prompt re-run (a fallback row), while an
    own-site description (``description_source`` NULL with a non-null
    ``description_short``) can never match and stays untouchable — the moat rule
    that own-website content is never overwritten by third-party-grounded text.
    ``description_long`` is set alongside (to the accepted profile or NULL, so a
    refreshed fallback row never keeps a stale long). One commit per company
    (the enrich_companies pattern), so a mid-run crash leaves earlier companies
    durably stamped/written.
    """
    if to_persist is not None:
        # Atomic check-and-set: refresh a PRISTINE row (both descriptions
        # NULL) or our own fallback stopgap; never any own-site text. The
        # long-NULL clause matters: a row with an own-site description_long
        # but no tagline (CI catch) must not gain a fallback tagline beside
        # own-site prose — the About attribution would misstate its source.
        written = (
            await session.execute(
                update(Company)
                .where(
                    Company.id == company.id,
                    or_(
                        (
                            Company.description_short.is_(None)
                            & Company.description_long.is_(None)
                        ),
                        Company.description_source == "fallback",
                    ),
                )
                .values(
                    description_short=to_persist,
                    description_long=long_to_persist,
                    description_source="fallback",
                )
                .returning(Company.id)
            )
        ).scalar_one_or_none()
        if written is not None:
            summary.persisted += 1
        else:
            summary.skipped_already_described += 1
    elif clear_stale:
        # A fallback row re-adjudicated to a DISTRUST-class null (entity
        # ambiguity, or content the current gates can't verify): the standing
        # description is text this version won't stand behind — clear it
        # rather than display it (review catch). Availability-class nulls
        # (insufficient/funding-only evidence) deliberately KEEP the old
        # description: it was grounded when written and the evidence may be
        # transiently thinner (e.g. a wikidata fetch failure this run).
        cleared = (
            await session.execute(
                update(Company)
                .where(
                    Company.id == company.id,
                    Company.description_source == "fallback",
                )
                .values(
                    description_short=None,
                    description_long=None,
                    description_source=None,
                )
                .returning(Company.id)
            )
        ).scalar_one_or_none()
        if cleared is not None:
            summary.stale_cleared += 1
    company.describe_fallback_prompt_version = PROMPT_VERSION
    await session.commit()


async def run_describe_fallback(
    session: AsyncSession,
    *,
    user_agent: str,
    limit: int | None = 20,
    dry_run: bool = True,
) -> DescribeFallbackSummary:
    """Third-party-grounded descriptions for the unscrapable residue.

    Cohort: shown companies (``exclusion_reason IS NULL``) that are either
    description-less (``description_short IS NULL``) OR carry this stage's own
    fallback stopgap (``description_source = 'fallback'``, refreshable on a
    prompt re-run), NO readable own page (no ``raw_pages`` row with >=
    ``_MIN_RAW_PAGE_CHARS`` of content — so an own-site ``description_long``,
    which implies scraped pages, is never in-cohort), AND
    whose ``describe_fallback_prompt_version`` is NULL or below the current
    ``PROMPT_VERSION`` (version-gated idempotency — a prompt bump re-selects
    everyone, stamped rows never re-bill). Prominence-ordered so a bounded
    ``--limit`` covers marquee residue first. Per company: assemble Wikidata +
    corroborated-news evidence, and — when there is any — send ONE
    ``describe_fallback`` LLM call, then verify the grounding descriptor AND the
    token-level claim check (M1) against the evidence.

    ``dry_run`` (default) writes nothing and returns the yield tally. Apply
    (``dry_run=False``) additionally persists ``description_short`` +
    ``description_source='fallback'`` for a grounded, non-low-confidence,
    claim-checked description (re-checking ``description_short IS NULL`` right
    before the write), and stamps every completed adjudication (and a clean
    skipped-no-evidence) per company. ``LLMRateLimitError`` breaks the loop
    (don't keep hammering a tripped quota — the un-stamped remainder re-selects
    next run); other per-company LLM errors are counted and leave the company
    un-stamped (re-eligible).
    """
    summary = DescribeFallbackSummary(dry_run=dry_run, prompt_version=PROMPT_VERSION)
    sample_cap = limit if limit is not None else _MAX_SAMPLES

    # The unscrapable / website-less residue: shown, description-less, lacking
    # any raw_page with real content, and not yet stamped at the current prompt
    # version. NOT exists() over the length floor is the "no readable own page"
    # clause; the version gate is the idempotency key (mirrors --redescribe-outdated).
    stmt = (
        select(Company)
        .where(
            Company.exclusion_reason.is_(None),
            # PRISTINE residue (no description of any kind) OR a fallback row
            # eligible to refresh its own stopgap on a prompt re-run. Any
            # own-site text — short, or a long without a tagline (CI catch:
            # such a row must not gain a fallback tagline beside own-site
            # prose) — is excluded here and, belt-and-suspenders, by the
            # persist WHERE.
            or_(
                (
                    Company.description_short.is_(None)
                    & Company.description_long.is_(None)
                ),
                Company.description_source == "fallback",
            ),
            not_(
                exists().where(
                    RawPage.company_id == Company.id,
                    func.length(RawPage.content) >= _MIN_RAW_PAGE_CHARS,
                )
            ),
            or_(
                Company.describe_fallback_prompt_version.is_(None),
                Company.describe_fallback_prompt_version < PROMPT_VERSION,
            ),
        )
        .order_by(
            Company.latest_round_amount.desc().nulls_last(),
            Company.funding_round_count.desc(),
            Company.id,
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    companies = list((await session.execute(stmt)).scalars().all())

    async with WikidataClient(user_agent) as wd:
        for company in companies:
            summary.cohort_selected += 1

            # Evidence A: Wikidata facts (free, un-Cloudflared).
            facts = await wd.entity_facts(
                company.name, company_country=company.hq_country
            )
            wiki_lines = _wikidata_lines(facts) if facts is not None else []
            if facts is not None and wiki_lines:
                summary.wikidata_hits += 1

            # Evidence B: corroborated recent news. Snapshot guard_errors around
            # the call: a company whose evidence assembly hit an LLM guard error
            # must NOT be stamped (it might have had evidence we couldn't see), so
            # it re-selects next run.
            guard_errors_before = summary.guard_errors
            survivors = await _surviving_articles(session, company, summary)
            had_guard_error = summary.guard_errors > guard_errors_before
            article_blocks = [_article_block(a) for a in survivors]

            evidence = _assemble_evidence(wiki_lines, article_blocks)
            # DISTINCT sources: wikidata plus distinct article HOSTS (two
            # articles from one outlet are one source — the long-profile
            # evidence bar must mean three independent voices; review catch).
            # Count by the stored OUTLET name first, URL host as fallback:
            # Google News-syndicated coverage stores news.google.com URLs for
            # EVERY outlet (the first profile run collapsed blue-origin's
            # multi-outlet coverage to one "host" and wrote ZERO longs), while
            # article.source carries the real publication.
            survivor_outlets = {
                key
                for a in survivors
                if (key := ((a.source or "").strip().lower() or hostname(a.url)))
            }
            evidence_sources = (1 if wiki_lines else 0) + len(survivor_outlets)

            if not evidence.strip():
                # No wikidata hit AND no surviving article → nothing to ground a
                # description on. Skip without an LLM call.
                summary.skipped_no_evidence += 1
                if len(summary.samples) < sample_cap:
                    summary.samples.append(
                        CompanySample(
                            slug=company.slug,
                            evidence_sources=0,
                            wikidata=False,
                            null_reason="no_evidence",
                        )
                    )
                # A CLEAN skip (no evidence today → no evidence tomorrow) stamps
                # so it isn't re-billed; a prompt bump re-selects it anyway. A
                # skip caused by a guard LLM error does NOT stamp (re-eligible).
                if not dry_run and not had_guard_error:
                    await _persist_company(session, company, None, None, summary)
                continue

            prompt = build_prompt(company_name=company.name, evidence=evidence)
            try:
                result = await complete_json(prompt, DescribeFallbackResult)
            except LLMRateLimitError as exc:
                logger.warning(
                    "describe-fallback: LLM rate-limited on %s — stopping the run "
                    "(remaining residue is picked up next run). Raw error: %s",
                    company.slug,
                    exc,
                )
                break
            except LLMError as exc:
                logger.warning(
                    "describe-fallback: LLM error for %s — skipping: %s",
                    company.slug,
                    exc,
                )
                summary.errors += 1
                continue
            summary.llm_calls += 1

            sample, to_persist, long_to_persist = _adjudicate_result(
                company.name,
                company.slug,
                result,
                evidence,
                evidence_sources,
                summary,
            )
            sample.evidence_sources = evidence_sources
            sample.wikidata = bool(wiki_lines)
            if len(summary.samples) < sample_cap:
                summary.samples.append(sample)

            # Every completed adjudication stamps (described, deliberate null,
            # descriptor/claim rejection alike) so it isn't re-billed; only a
            # grounded, non-low, claim-checked description also writes text.
            # EXCEPT: a NULL outcome adjudicated on PARTIAL evidence (a guard
            # LLM error dropped articles this run) does not stamp — the missing
            # articles may describe the company, so the row stays re-eligible
            # rather than being deferred to the next prompt bump (review catch;
            # a described outcome still stamps — richer evidence can only have
            # agreed). Persistent guard errors re-bill ~$0.0005/run — accepted.
            if not dry_run and not (to_persist is None and had_guard_error):
                # Distrust-class nulls (possibly-wrong-entity or unverifiable
                # content) clear a standing fallback description; availability-
                # class nulls keep it (see _persist_company).
                distrust_null = to_persist is None and sample.null_reason in (
                    "entity_ambiguity",
                    "descriptor_not_in_evidence",
                    "claims_not_grounded",
                )
                await _persist_company(
                    session,
                    company,
                    to_persist,
                    long_to_persist,
                    summary,
                    clear_stale=distrust_null,
                )

    logger.info(
        "describe-fallback: cohort=%d wikidata_hits=%d articles_seen=%d "
        "corroborated=%d guard_dropped=%d llm_calls=%d described=%d "
        "long_written=%d long_below_bar=%d long_not_grounded=%d "
        "descriptor_not_in_evidence=%d claims_not_grounded=%d low_conf=%d "
        "skipped_no_evidence=%d persisted=%d skipped_already_described=%d "
        "non_us_suspects=%d errors=%d dry_run=%s",
        summary.cohort_selected,
        summary.wikidata_hits,
        summary.articles_seen,
        summary.articles_corroborated,
        summary.guard_dropped,
        summary.llm_calls,
        summary.described,
        summary.long_written,
        summary.long_below_evidence_bar,
        summary.long_claims_not_grounded,
        summary.descriptor_not_in_evidence,
        summary.claims_not_grounded,
        summary.low_confidence,
        summary.skipped_no_evidence,
        summary.persisted,
        summary.skipped_already_described,
        len(summary.non_us_suspects),
        summary.errors,
        dry_run,
    )
    return summary


def _adjudicate_long(
    company_name: str,
    description_long: str,
    evidence: str,
    evidence_sources: int,
    sample: CompanySample,
    summary: DescribeFallbackSummary,
) -> str | None:
    """Post-validate an optional LONG profile into the profile-to-persist.

    Reached ONLY on the persist path (a grounded, non-low-confidence tagline),
    so a profile is only ever considered when it will actually be written. Two
    evidence-proportional gates, both dropping the LONG ONLY (the short stands):

    1. **rich-evidence bar** — a multi-paragraph profile is licensed only by
       genuinely rich evidence: at least THREE distinct sources (Wikidata counts
       as one). Below the bar → ``long_below_evidence_bar``.
    2. **token-level claim check (M1)** — the same ``_claim_is_grounded`` floor
       the short passes, applied to the whole profile so a fluent paraphrase
       that drifts past the evidence is caught → ``long_claims_not_grounded``.
    """
    if evidence_sources < 3:
        summary.long_below_evidence_bar += 1
        return None
    if not _claim_is_grounded(description_long, company_name, evidence):
        summary.long_claims_not_grounded += 1
        return None
    summary.long_written += 1
    sample.description_long = truncate_to_chars(description_long, _SAMPLE_LONG_CHARS)
    return description_long


def _adjudicate_result(
    company_name: str,
    slug: str,
    result: DescribeFallbackResult,
    evidence: str,
    evidence_sources: int,
    summary: DescribeFallbackSummary,
) -> tuple[CompanySample, str | None, str | None]:
    """Post-validate one LLM result into (sample, short-to-persist, long-to-persist).

    The prompt's own validator already dropped a description missing its
    descriptor / over length. Here two MOAT checks back it, both against the
    shown evidence, never the model's word:

    1. **descriptor-in-evidence** — the echoed grounding descriptor must appear
       in the evidence (``descriptor_not_in_evidence`` otherwise).
    2. **token-level claim check (M1)** — most of the description's own content
       words must appear in the evidence, so a fluent paraphrase that grounds its
       descriptor but invents the rest of the clause is caught
       (``claims_not_grounded`` otherwise).

    The first returned string is the SHORT description to PERSIST — non-None only
    for a grounded, claim-checked, non-low-confidence description. The second is
    the optional LONG profile to persist (see ``_adjudicate_long``), considered
    only on the persist path. ``low`` confidence is tallied and surfaced but
    never persisted (neither short nor long). A produced description that reads
    non-US is flagged in ``non_us_suspects`` (persisted normally — the flag is
    for the ops exclusion flow, it does not block the write).
    """
    sample = CompanySample(slug=slug)
    if result.description_short is not None:
        if not _descriptor_in_evidence(result.grounding_descriptor, evidence):
            # The echo is not grounded in the evidence — discard it.
            summary.descriptor_not_in_evidence += 1
            sample.null_reason = "descriptor_not_in_evidence"
            return sample, None, None
        if not _claim_is_grounded(result.description_short, company_name, evidence):
            # The descriptor grounds but the sentence as a whole drifts past the
            # evidence — discard it (M1).
            summary.claims_not_grounded += 1
            sample.null_reason = "claims_not_grounded"
            return sample, None, None
        summary.described += 1
        sample.description_short = result.description_short
        sample.grounding_descriptor = result.grounding_descriptor
        sample.confidence = result.confidence
        if _looks_non_us(result.description_short):
            summary.non_us_suspects.append(slug)
        if result.confidence == "low":
            summary.low_confidence += 1
            return sample, None, None  # described but never persisted (nor its long)
        # Only a persistable (non-low) tagline gets an optional LONG profile.
        long_to_persist = (
            _adjudicate_long(
                company_name,
                result.description_long,
                evidence,
                evidence_sources,
                sample,
                summary,
            )
            if result.description_long is not None
            else None
        )
        return sample, result.description_short, long_to_persist

    # The model returned an explicit null — record its reason.
    reason = result.null_reason or "insufficient_evidence"
    sample.null_reason = reason
    if reason == "no_nonfunding_descriptor":
        summary.null_no_descriptor += 1
    elif reason == "entity_ambiguity":
        summary.null_ambiguity += 1
    else:
        summary.null_insufficient += 1
    return sample, None, None


def render_yield_table(summary: DescribeFallbackSummary) -> str:
    """Render the run summary as GitHub-flavored markdown for the step summary.

    Dry-run: the yield + fabrication proxies + a go/no-go guide. Apply: the same
    yield plus the persistence counters (written, skipped-already-described) and
    the non-US suspect list for the ops exclusion flow.
    """
    cohort = summary.cohort_selected
    described_pct = (summary.described / cohort * 100) if cohort else 0.0
    mode = "dry-run probe" if summary.dry_run else "apply"
    lines: list[str] = []
    lines.append(f"## describe-fallback — {mode}")
    lines.append("")
    lines.append(f"- **Prompt version:** `{summary.prompt_version}`")
    lines.append(f"- **Cohort selected:** {cohort}")
    lines.append(f"- **Wikidata facts hits:** {summary.wikidata_hits}")
    lines.append(
        f"- **Articles:** {summary.articles_seen} seen, "
        f"{summary.articles_corroborated} corroborated, "
        f"{summary.guard_dropped} guard-dropped, {summary.guard_errors} guard-errors"
    )
    if summary.guard_rate_limited:
        lines.append("- **Guard rate-limited:** yes (stopped adjudicating mid-run)")
    lines.append(f"- **Skipped (no evidence):** {summary.skipped_no_evidence}")
    lines.append(f"- **LLM calls:** {summary.llm_calls}")
    lines.append(
        f"- **Described (grounded):** {summary.described} ({described_pct:.0f}% of cohort)"
    )
    lines.append(
        f"- **Long profiles written (evidence-proportional):** {summary.long_written}"
    )
    lines.append(
        f"- **Long dropped — below evidence bar / claims not grounded:** "
        f"{summary.long_below_evidence_bar} / {summary.long_claims_not_grounded}"
    )
    lines.append(
        f"- **Low-confidence (would NOT persist):** {summary.low_confidence}"
    )
    lines.append(
        f"- **Null — no descriptor / insufficient / ambiguity:** "
        f"{summary.null_no_descriptor} / {summary.null_insufficient} / "
        f"{summary.null_ambiguity}"
    )
    lines.append(
        f"- **Descriptor not in evidence (echo discarded):** "
        f"{summary.descriptor_not_in_evidence}"
    )
    lines.append(
        f"- **Claims not grounded (M1 — sentence drift discarded):** "
        f"{summary.claims_not_grounded}"
    )
    if not summary.dry_run:
        lines.append(
            f"- **Persisted (description_short written):** {summary.persisted}"
        )
        lines.append(
            f"- **Skipped (already described mid-run):** "
            f"{summary.skipped_already_described}"
        )
    if summary.non_us_suspects:
        suspects = ", ".join(summary.non_us_suspects)
        lines.append(
            f"- **Non-US suspects (persisted; ops-exclusion review):** "
            f"{len(summary.non_us_suspects)} — {suspects}"
        )
    lines.append(f"- **Errors:** {summary.errors}")
    lines.append("- **Cost:** see the LLM-usage block below (DeepSeek, paid).")
    lines.append("")
    lines.append("### Per-company detail")
    lines.append(
        "| Company | Sources | WD | Description | Profile | Descriptor | Conf | Null |"
    )
    lines.append("|---|--:|:--:|---|---|---|:--:|---|")
    for s in summary.samples:
        wd = "✓" if s.wikidata else ""
        desc = (s.description_short or "—").replace("|", "\\|")
        profile = (s.description_long or "—").replace("|", "\\|").replace("\n", " ")
        descriptor = (s.grounding_descriptor or "—").replace("|", "\\|")
        conf = s.confidence or "—"
        null = s.null_reason or ""
        lines.append(
            f"| {s.slug} | {s.evidence_sources} | {wd} | {desc} "
            f"| {profile} | {descriptor} | {conf} | {null} |"
        )
    if summary.dry_run:
        lines.append("")
        lines.append("### Go / no-go")
        lines.append(
            "- Grounded descriptions on marquee residue + descriptor-in-evidence "
            "holding + near-zero funding-only nulls leaking through → run the "
            "persisting apply path (--apply)."
        )
        lines.append(
            "- Frequent descriptor_not_in_evidence, claims_not_grounded, "
            "low-confidence, or entity-ambiguity nulls → tighten the prompt / "
            "evidence assembly before persisting."
        )
    return "\n".join(lines)
