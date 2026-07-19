"""describe-fallback — third-party-grounded description_short (PROBE, dry-run only).

The MEASURE-FIRST probe for the owner-approved "describe-fallback" (BACKLOG
2026-07-19 "missing-data residue"; the deferred option "A" re-opened). Normal
descriptions are written ONLY from a company's own scraped pages; this stage
targets the residue that has NO readable own pages — companies nous shows but
cannot describe because the homepage is Cloudflare-403'd or absent. For each, it
assembles third-party EVIDENCE nous already holds — Wikidata entity facts plus
entity-guard-corroborated news coverage — and asks the ``describe_fallback``
prompt for a SHORT factual description, gated hard: every clause traceable to the
shown evidence, null over thin, and a code-checked grounding descriptor.

This PR is the probe: **dry-run only**. It runs the whole pipeline (cohort →
evidence → LLM → post-validation) and reports a yield table so the owner can see
what fraction of the residue gets a grounded description, at what confidence, and
how often the model tries to describe on funding facts alone. It writes NOTHING —
no ``description_short``, no provenance, no stamp (there is no migration in this
PR). ``dry_run=False`` raises: the apply path lands in a later PR after the prod
dry run clears the quality gate.

The moat rule (this is a GENERATIVE stage, so the gates are stricter than
anywhere else): the description is never trusted on the model's word. Two
code-level checks back the prompt's own rules — the prompt's validator drops a
description lacking a grounding descriptor, and this stage additionally verifies
that the echoed descriptor actually appears in the evidence text (an ungrounded
echo is discarded with its own counter). Wrong-entity news is filtered before it
becomes evidence by the same cheap corroboration + entity guard the ingest path
uses. Cost: one DeepSeek call per company that has any evidence (skipped
otherwise); the exact spend lands in the ``emit_run_telemetry`` block.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from sqlalchemy import exists, func, not_, select
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

# Per-article excerpt length in the evidence block. Descriptors live in the
# headline + lede; a few hundred chars carry them without burning the budget.
_ARTICLE_EXCERPT_CHARS = 400

# Safety cap on the per-company sample list when --limit is unbounded.
_MAX_SAMPLES = 50


class CompanySample(BaseModel):
    """One company's probe outcome — the per-company review row."""

    slug: str
    evidence_sources: int = 0  # distinct citations fed to the LLM (wikidata + articles)
    wikidata: bool = False  # a Wikidata facts hit contributed evidence
    description_short: str | None = None
    grounding_descriptor: str | None = None
    confidence: str | None = None
    null_reason: str | None = None  # why no (persistable) description, when so


class DescribeFallbackSummary(BaseModel):
    """Stage summary — feeds the yield table and telemetry. Dry-run only."""

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
    null_no_descriptor: int = 0  # model returned null: no non-funding descriptor
    null_insufficient: int = 0  # model returned null: insufficient evidence
    null_ambiguity: int = 0  # model returned null: entity ambiguity
    descriptor_not_in_evidence: int = 0  # echoed descriptor absent from evidence
    low_confidence: int = 0  # described but confidence=='low' (would NOT persist)
    errors: int = 0  # per-company LLM errors (non-rate-limit)
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


def _descriptor_in_evidence(descriptor: str | None, evidence: str) -> bool:
    """Does ``descriptor`` appear in ``evidence`` case-insensitively after
    whitespace normalization? The moat-critical post-validation: the model's
    echoed grounding descriptor is verified against the shown evidence, never
    trusted on its word (the same grounded-quote discipline as source_verification).
    """
    if descriptor is None or not descriptor.strip():
        return False
    norm_desc = " ".join(descriptor.lower().split())
    norm_evidence = " ".join(evidence.lower().split())
    return norm_desc in norm_evidence


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
        combined = body if title.strip() and title in body else f"{title}. {body}"

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


async def run_describe_fallback(
    session: AsyncSession,
    *,
    user_agent: str,
    limit: int | None = 20,
    dry_run: bool = True,
) -> DescribeFallbackSummary:
    """Probe third-party-grounded descriptions for the unscrapable residue.

    Cohort: shown companies (``exclusion_reason IS NULL``) with NO description
    (``description_short`` and ``description_long`` both NULL) and NO readable own
    page (no ``raw_pages`` row with >= ``_MIN_RAW_PAGE_CHARS`` of content),
    prominence-ordered so a bounded ``--limit`` covers marquee residue first. Per
    company: assemble Wikidata + corroborated-news evidence, and — when there is
    any — send ONE ``describe_fallback`` LLM call, then verify the grounding
    descriptor against the evidence. Writes NOTHING (probe only); returns the
    yield tally.

    ``dry_run=False`` raises ``ValueError`` — the apply path is not built (no
    migration in this PR). ``LLMRateLimitError`` breaks the loop (don't keep
    hammering a tripped quota); other per-company LLM errors are counted and the
    loop continues.
    """
    if not dry_run:
        # The CLI catches this and surfaces it as a ClickException; the workflow
        # refuses dry_run=false before it ever reaches here.
        raise ValueError("describe-fallback apply path not built yet — probe only")

    summary = DescribeFallbackSummary(dry_run=dry_run, prompt_version=PROMPT_VERSION)
    sample_cap = limit if limit is not None else _MAX_SAMPLES

    # The unscrapable / website-less residue: shown, description-less, and lacking
    # any raw_page with real content. NOT exists() over the length floor is the
    # "no readable own page" clause.
    stmt = (
        select(Company)
        .where(
            Company.exclusion_reason.is_(None),
            Company.description_short.is_(None),
            Company.description_long.is_(None),
            not_(
                exists().where(
                    RawPage.company_id == Company.id,
                    func.length(RawPage.content) >= _MIN_RAW_PAGE_CHARS,
                )
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

            # Evidence B: corroborated recent news.
            survivors = await _surviving_articles(session, company, summary)
            article_blocks = [_article_block(a) for a in survivors]

            evidence = _assemble_evidence(wiki_lines, article_blocks)
            evidence_sources = (1 if wiki_lines else 0) + len(survivors)

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

            sample = _adjudicate_result(company.slug, result, evidence, summary)
            sample.evidence_sources = evidence_sources
            sample.wikidata = bool(wiki_lines)
            if len(summary.samples) < sample_cap:
                summary.samples.append(sample)

    logger.info(
        "describe-fallback: cohort=%d wikidata_hits=%d articles_seen=%d "
        "corroborated=%d guard_dropped=%d llm_calls=%d described=%d "
        "descriptor_not_in_evidence=%d low_conf=%d skipped_no_evidence=%d errors=%d",
        summary.cohort_selected,
        summary.wikidata_hits,
        summary.articles_seen,
        summary.articles_corroborated,
        summary.guard_dropped,
        summary.llm_calls,
        summary.described,
        summary.descriptor_not_in_evidence,
        summary.low_confidence,
        summary.skipped_no_evidence,
        summary.errors,
    )
    return summary


def _adjudicate_result(
    slug: str,
    result: DescribeFallbackResult,
    evidence: str,
    summary: DescribeFallbackSummary,
) -> CompanySample:
    """Post-validate one LLM result into a sample + summary counters.

    The prompt's own validator already dropped a description missing its
    descriptor / over length; here the MOAT check is the descriptor-in-evidence
    verification: a description whose echoed grounding descriptor does not actually
    appear in the evidence is discarded (``descriptor_not_in_evidence``), never
    trusted. A surviving description is tallied; ``low`` confidence is flagged
    separately because it would NOT persist once the apply path lands.
    """
    sample = CompanySample(slug=slug)
    if result.description_short is not None:
        if not _descriptor_in_evidence(result.grounding_descriptor, evidence):
            # The echo is not grounded in the evidence — discard it.
            summary.descriptor_not_in_evidence += 1
            sample.null_reason = "descriptor_not_in_evidence"
            return sample
        summary.described += 1
        sample.description_short = result.description_short
        sample.grounding_descriptor = result.grounding_descriptor
        sample.confidence = result.confidence
        if result.confidence == "low":
            summary.low_confidence += 1
        return sample

    # The model returned an explicit null — record its reason.
    reason = result.null_reason or "insufficient_evidence"
    sample.null_reason = reason
    if reason == "no_nonfunding_descriptor":
        summary.null_no_descriptor += 1
    elif reason == "entity_ambiguity":
        summary.null_ambiguity += 1
    else:
        summary.null_insufficient += 1
    return sample


def render_yield_table(summary: DescribeFallbackSummary) -> str:
    """Render the probe summary as GitHub-flavored markdown for the step summary."""
    cohort = summary.cohort_selected
    described_pct = (summary.described / cohort * 100) if cohort else 0.0
    lines: list[str] = []
    lines.append("## describe-fallback — dry-run probe")
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
    lines.append(f"- **Errors:** {summary.errors}")
    lines.append("- **Cost:** see the LLM-usage block below (DeepSeek, paid).")
    lines.append("")
    lines.append("### Per-company detail")
    lines.append("| Company | Sources | WD | Description | Descriptor | Conf | Null |")
    lines.append("|---|--:|:--:|---|---|:--:|---|")
    for s in summary.samples:
        wd = "✓" if s.wikidata else ""
        desc = (s.description_short or "—").replace("|", "\\|")
        descriptor = (s.grounding_descriptor or "—").replace("|", "\\|")
        conf = s.confidence or "—"
        null = s.null_reason or ""
        lines.append(
            f"| {s.slug} | {s.evidence_sources} | {wd} | {desc} "
            f"| {descriptor} | {conf} | {null} |"
        )
    lines.append("")
    lines.append("### Go / no-go")
    lines.append(
        "- Grounded descriptions on marquee residue + descriptor-in-evidence "
        "holding + near-zero funding-only nulls leaking through → build the "
        "persisting apply path (write description_short + a third-party "
        "provenance stamp, skip low-confidence)."
    )
    lines.append(
        "- Frequent descriptor_not_in_evidence, low-confidence, or entity-ambiguity "
        "nulls → tighten the prompt / evidence assembly before persisting."
    )
    return "\n".join(lines)
