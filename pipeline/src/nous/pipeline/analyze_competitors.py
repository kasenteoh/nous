"""analyze-competitors pipeline stage (M4).

For each enriched, industry-classified company with no recent competitors
analysis, call the LLM with the target description + a peer list of up to 50
same-industry companies, and write the ranked competitor set to the
`competitors` table.

Idempotency:
- Replace-style writes: each run for a company DELETEs existing rows for that
  company_id then INSERTs the new ranked set in one transaction.
- TTL gate (default 25 days): a company is re-analyzed only when no rows exist
  or when MAX(updated_at) is older than the TTL.

Quota discipline (spec §11):
- Hard cap on companies processed per run (default 500) to bound per-run
  LLM spend on DeepSeek.
- On LLMRateLimitError, stop the loop immediately — same pattern as
  extract-funding.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from nous.db.models import Company, Competitor, NewsArticle
from nous.llm.client import LLMError, LLMParseError, LLMRateLimitError, complete_json
from nous.llm.prompts.competitor_analysis import (
    MAX_PEERS,
    CompetitorAnalysis,
    Peer,
    Target,
    build_prompt,
)
from nous.llm.prompts.competitor_analysis import (
    PROMPT_VERSION as COMPETITOR_PROMPT_VERSION,
)
from nous.llm.prompts.competitor_candidates import (
    MAX_ARTICLES,
    CompetitorCandidates,
    TechCrunchArticle,
    build_candidates_prompt,
)
from nous.util.slugify import normalize_name

logger = logging.getLogger(__name__)

_TECHCRUNCH_SOURCE = "techcrunch"
_LLM_SOURCE = "llm_inferred"

# How many companies' LLM work to run at once. Each company makes up to two
# DeepSeek calls (candidate pass + analysis pass), both network-bound; fanning a
# batch out collapses wall-clock roughly ``concurrency``-fold. Only the LLM work
# is parallelized — every DB read/write stays strictly sequential on the single
# passed-in session, so there is no added DB concurrency and the existing
# per-company commit cadence / idempotency is unchanged.
_DEFAULT_CONCURRENCY: int = 5


class AnalyzeCompetitorsSummary(BaseModel):
    companies_analyzed: int = 0
    competitors_written: int = 0
    competitors_linked: int = 0
    competitors_unlinked: int = 0
    competitors_from_techcrunch: int = 0
    competitors_from_llm: int = 0
    llm_failures: int = 0
    skipped_rate_limited: int = 0


# ---------------------------------------------------------------------------
# Eligibility query
# ---------------------------------------------------------------------------


async def fetch_eligible_companies(
    session: AsyncSession,
    *,
    limit: int,
    ttl_days: int,
) -> list[Company]:
    """Return companies eligible for competitor analysis.

    A company is eligible when:
    - description_long IS NOT NULL
    - industry_group IS NOT NULL
    - No competitors row exists for it, OR MAX(competitors.updated_at) is older
      than `ttl_days` days ago.
    """
    cutoff = datetime.now(UTC) - timedelta(days=ttl_days)

    # Subquery: most-recent competitors.updated_at per company_id.
    last_analyzed = (
        select(
            Competitor.company_id,
            func.max(Competitor.updated_at).label("last_analyzed_at"),
        )
        .group_by(Competitor.company_id)
        .subquery()
    )

    stmt = (
        select(Company)
        .outerjoin(last_analyzed, Company.id == last_analyzed.c.company_id)
        .where(Company.description_long.is_not(None))
        .where(Company.industry_group.is_not(None))
        .where(Company.exclusion_reason.is_(None))
        .where(
            (last_analyzed.c.last_analyzed_at.is_(None))
            | (last_analyzed.c.last_analyzed_at < cutoff)
        )
        .order_by(
            last_analyzed.c.last_analyzed_at.asc().nullsfirst(),
            Company.name.asc(),
        )
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# Peer-list query
# ---------------------------------------------------------------------------


async def fetch_peers(
    session: AsyncSession, *, target: Company, max_peers: int = MAX_PEERS
) -> list[Peer]:
    """Return up to `max_peers` companies in the same industry_group as `target`,
    excluding the target itself. Ordered by name for deterministic output."""
    stmt = (
        select(Company.name, Company.description_short)
        .where(Company.industry_group == target.industry_group)
        .where(Company.id != target.id)
        .where(Company.description_short.is_not(None))
        .order_by(Company.name.asc())
        .limit(max_peers)
    )
    rows = (await session.execute(stmt)).all()
    return [
        Peer(name=row.name, description_short=row.description_short or "")
        for row in rows
    ]


# ---------------------------------------------------------------------------
# TechCrunch evidence (pass 1 input)
# ---------------------------------------------------------------------------


async def fetch_techcrunch_articles(
    session: AsyncSession, *, company_id: UUID, max_articles: int = MAX_ARTICLES
) -> list[TechCrunchArticle]:
    """Return the company's most-recent TechCrunch articles (newest first).

    Matches on the news_articles.source hostname so any techcrunch.com article
    qualifies as competitor evidence for pass 1.
    """
    stmt = (
        select(NewsArticle.url, NewsArticle.raw_content)
        .where(NewsArticle.company_id == company_id)
        .where(NewsArticle.source.ilike("%techcrunch%"))
        .order_by(
            NewsArticle.published_date.desc().nulls_last(),
            NewsArticle.created_at.desc(),
        )
        .limit(max_articles)
    )
    rows = (await session.execute(stmt)).all()
    return [
        TechCrunchArticle(url=row.url, text=row.raw_content or "")
        for row in rows
        if (row.raw_content or "").strip()
    ]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def resolve_competitor_company_id(
    session: AsyncSession, *, name: str
) -> UUID | None:
    """Look up an indexed company by exact normalized_name match.

    Uses ``normalize_name`` from ``nous.util.slugify`` — the same helper that
    populates ``Company.normalized_name`` at insert/upsert time. This is what
    lets an LLM-emitted "OpenAI, Inc." resolve to the row whose stored
    normalized_name is "openai" (corporate suffix stripped, whitespace
    collapsed). Fuzzy match is deliberately deferred — spec §10 lists it as
    out-of-scope for M4.
    """
    normalized = normalize_name(name)
    if not normalized:
        return None
    stmt = select(Company.id).where(Company.normalized_name == normalized).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Per-company LLM work (no DB access — safe to run concurrently)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CompanyInputs:
    """DB-derived inputs for one company's LLM passes, gathered sequentially on
    the single session BEFORE any concurrent work begins."""

    company: Company
    tc_articles: list[TechCrunchArticle]
    peers: list[Peer]


@dataclass(slots=True)
class _AnalysisOutcome:
    """LLM-only result for one company (no DB access).

    Exactly one of the status flags is meaningful per outcome:
    - ``rate_limited``: a 429 was seen; the scheduler stops issuing more work.
    - ``llm_failure``: a parse/other LLM error — count + skip this company.
    - otherwise (``analysis`` is set): success; Phase 3 resolves + writes it.

    ``analysis`` / ``candidate_map`` / ``candidate_names`` carry forward the LLM
    output so Phase 3 can resolve names + persist with no further LLM calls.
    """

    candidate_map: dict[str, str]
    candidate_names: list[str]
    analysis: CompetitorAnalysis | None
    rate_limited: bool
    llm_failure: bool


async def _analyze_one(inputs: _CompanyInputs) -> _AnalysisOutcome:
    """Run a company's two LLM passes. No DB access, so a batch of these is safe
    to run concurrently against the shared LLM client. Returns a result object
    that Phase 3 turns into sequential DB writes.

    Mirrors the original serial control flow exactly:
    - Pass 1 (candidates) runs only when the company has TechCrunch coverage; a
      parse/other error there degrades to LLM-only competitors, a 429 aborts.
    - Pass 2 (analysis) always runs; a 429 aborts, a parse/other error skips.
    """
    company = inputs.company

    # --- Pass 1: pull competitor candidates from the company's TC coverage.
    # candidate_map: normalized competitor name -> source TechCrunch URL.
    candidate_map: dict[str, str] = {}
    candidate_names: list[str] = []
    if inputs.tc_articles:
        cand_prompt = build_candidates_prompt(
            target_name=company.name, articles=inputs.tc_articles
        )
        try:
            cand_result: CompetitorCandidates = await complete_json(
                cand_prompt, CompetitorCandidates
            )
        except LLMRateLimitError:
            logger.warning(
                "LLM rate limit hit during competitor-candidate extraction "
                "for %s — stopping loop.",
                company.name,
            )
            return _AnalysisOutcome(
                candidate_map={},
                candidate_names=[],
                analysis=None,
                rate_limited=True,
                llm_failure=False,
            )
        except (LLMParseError, LLMError) as exc:
            # Degrade gracefully: proceed with LLM-only competitors.
            logger.warning(
                "LLM error extracting TC competitor candidates for %s: %s",
                company.name,
                exc,
            )
            cand_result = CompetitorCandidates()
        for mention in cand_result.candidates:
            norm = normalize_name(mention.name)
            if not norm or norm in candidate_map:
                continue
            candidate_map[norm] = mention.article_url
            candidate_names.append(mention.name)

    # --- Pass 2: revalidate candidates + combine with LLM-inferred peers.
    target = Target(
        name=company.name,
        description_short=company.description_short or "",
        description_long=company.description_long or "",
        industry_group=company.industry_group or "",
    )
    prompt = build_prompt(
        target=target, peers=inputs.peers, tc_candidates=candidate_names
    )

    try:
        analysis: CompetitorAnalysis = await complete_json(prompt, CompetitorAnalysis)
    except LLMRateLimitError:
        logger.warning(
            "LLM rate limit hit while analyzing competitors for %s — "
            "stopping loop to avoid further quota exhaustion.",
            company.name,
        )
        return _AnalysisOutcome(
            candidate_map={},
            candidate_names=[],
            analysis=None,
            rate_limited=True,
            llm_failure=False,
        )
    except (LLMParseError, LLMError) as exc:
        logger.warning(
            "LLM error analyzing competitors for %s: %s", company.name, exc
        )
        return _AnalysisOutcome(
            candidate_map={},
            candidate_names=[],
            analysis=None,
            rate_limited=False,
            llm_failure=True,
        )

    return _AnalysisOutcome(
        candidate_map=candidate_map,
        candidate_names=candidate_names,
        analysis=analysis,
        rate_limited=False,
        llm_failure=False,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _persist_analysis(
    session: AsyncSession,
    summary: AnalyzeCompetitorsSummary,
    *,
    company: Company,
    outcome: _AnalysisOutcome,
    dry_run: bool,
) -> None:
    """Resolve an analysis outcome's competitor names + apply the replace-style
    write for one company. Runs sequentially on the single session — never from
    a concurrent task. Preserves the original resolution + write + commit logic
    byte-for-byte (only its inputs now arrive from a precomputed outcome)."""
    assert outcome.analysis is not None  # caller guards on success
    summary.companies_analyzed += 1

    # Resolve each competitor name to a company_id (None if unmatched) and
    # determine provenance: a competitor whose normalized name came from the
    # TechCrunch candidates is sourced to that article; everything else the
    # model added is "llm_inferred" (rendered as a *potential* competitor).
    # Resolve each competitor and collect in LLM order (sorted by rank).
    # The CompetitorAnalysis validator already guarantees contiguous 1..N
    # ranks; this re-rank is belt-and-braces so a future relaxation of that
    # validation can't violate the unique (company_id, rank) constraint.
    # Self-referential edges (competitor resolves to the target company itself)
    # are dropped before rank assignment so ranks remain gap-free over survivors.
    llm_ordered = sorted(outcome.analysis.competitors, key=lambda x: x.rank)
    resolved: list[tuple[UUID | None, str, str, str, int, str, str | None]] = []
    contiguous_rank = 0
    for c in llm_ordered:
        cid = await resolve_competitor_company_id(session, name=c.name)
        # Guard: drop self-referential edges. The LLM occasionally lists the
        # target company among its own competitors (or a normalized_name collision
        # maps a competitor name back to the target). Inserting such a row
        # violates ck_competitors_no_self_reference, so we drop it here.
        if cid is not None and cid == company.id:
            logger.warning(
                "Dropping self-referential competitor '%s' for company %s (%s)",
                c.name,
                company.name,
                company.id,
            )
            continue
        contiguous_rank += 1
        article_url = outcome.candidate_map.get(normalize_name(c.name))
        if article_url is not None:
            source, source_url = _TECHCRUNCH_SOURCE, article_url
        else:
            source, source_url = _LLM_SOURCE, None
        resolved.append(
            (cid, c.name, c.description, c.reasoning, contiguous_rank, source, source_url)
        )

    if dry_run:
        for *_, source, _su in resolved:
            if source == _TECHCRUNCH_SOURCE:
                summary.competitors_from_techcrunch += 1
            else:
                summary.competitors_from_llm += 1
        return

    # Replace-style write, committed PER COMPANY. The SAVEPOINT via
    # begin_nested() isolates a single company's write failure; the commit
    # then persists it incrementally — crash-safe + resumable, and a later
    # rate-limit break keeps everything written so far. (A previous version
    # only flushed here and never committed; since the CLI opens a plain
    # AsyncSessionLocal() with no auto-commit, every run was rolled back on
    # close and the competitors table stayed empty.)
    async with session.begin_nested():
        await session.execute(
            delete(Competitor).where(Competitor.company_id == company.id)
        )
        now = datetime.now(UTC)
        for cid, name, desc, reasoning, rank, source, source_url in resolved:
            session.add(
                Competitor(
                    company_id=company.id,
                    competitor_company_id=cid,
                    competitor_name=name,
                    description=desc,
                    reasoning=reasoning,
                    rank=rank,
                    source=source,
                    source_url=source_url,
                    # Provenance stamp: the pass-2 (competitor_analysis)
                    # prompt authored every persisted row — techcrunch-sourced
                    # candidates are revalidated and rewritten by it, so its
                    # version is the one that matters for re-run queries.
                    prompt_version=COMPETITOR_PROMPT_VERSION,
                    updated_at=now,
                )
            )
            summary.competitors_written += 1
            if cid is not None:
                summary.competitors_linked += 1
            else:
                summary.competitors_unlinked += 1
            if source == _TECHCRUNCH_SOURCE:
                summary.competitors_from_techcrunch += 1
            else:
                summary.competitors_from_llm += 1
    await session.commit()


async def run_analyze_competitors(
    session: AsyncSession,
    *,
    limit: int = 500,
    ttl_days: int = 25,
    dry_run: bool = False,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> AnalyzeCompetitorsSummary:
    """Analyze competitors for eligible companies, fanning the per-company LLM
    work out with bounded concurrency.

    Three phases keep the single AsyncSession concurrency-safe:
    1. Sequentially gather each company's DB-derived LLM inputs (TechCrunch
       articles + peer list) on the session.
    2. Run the two LLM passes per company concurrently, bounded by a
       ``concurrency``-wide semaphore. These tasks never touch the session.
    3. Sequentially resolve competitor names + apply the replace-style write +
       commit per company, in the deterministic eligibility order.

    Rate-limit handling matches the original serial loop: the FIRST 429 stops
    further LLM scheduling, everything already extracted is still written, and
    ``skipped_rate_limited`` is recorded once. Companies that didn't run because
    of the stop stay eligible next run (no rows written, TTL gate unchanged).
    Per-company parse/other LLM errors are counted and skipped individually.
    """
    summary = AnalyzeCompetitorsSummary()

    companies = await fetch_eligible_companies(
        session, limit=limit, ttl_days=ttl_days
    )
    if not companies:
        return summary

    # Phase 1: gather every company's LLM inputs sequentially on the session.
    # (DB reads — must not happen inside the concurrent tasks below, since a
    # single AsyncSession is not concurrency-safe.)
    inputs: list[_CompanyInputs] = []
    for company in companies:
        tc_articles = await fetch_techcrunch_articles(session, company_id=company.id)
        peers = await fetch_peers(session, target=company)
        inputs.append(
            _CompanyInputs(company=company, tc_articles=tc_articles, peers=peers)
        )

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(one: _CompanyInputs) -> _AnalysisOutcome:
        async with semaphore:
            return await _analyze_one(one)

    # Phases 2 + 3, batched. Processing in batches lets a 429 stop scheduling
    # the REMAINING batches promptly (instead of fanning out every company's
    # LLM call up front), preserving the serial loop's "stop on first rate
    # limit" cost discipline while still parallelizing within a batch.
    batch_size = max(1, concurrency)
    stop = False
    for start in range(0, len(inputs), batch_size):
        if stop:
            break
        batch = inputs[start : start + batch_size]

        # Phase 2: run this batch's LLM work concurrently (no DB access).
        outcomes = await asyncio.gather(*(_bounded(one) for one in batch))

        # Phase 3: apply results sequentially on the single session, in
        # eligibility order. A 429 anywhere in the batch stops scheduling
        # further batches; outcomes that completed before/alongside it are
        # still persisted (matching the serial "keep everything written so
        # far" guarantee).
        for one, outcome in zip(batch, outcomes, strict=True):
            if outcome.rate_limited:
                stop = True
                continue
            if outcome.llm_failure:
                summary.llm_failures += 1
                continue
            # Capture the name before the try block — accessing ORM attributes
            # after session.rollback() expires them, causing a lazy-load attempt
            # that fails (mirrors the StaleDataError pattern in enrich_companies).
            company_name = one.company.name
            try:
                await _persist_analysis(
                    session,
                    summary,
                    company=one.company,
                    outcome=outcome,
                    dry_run=dry_run,
                )
            except (IntegrityError, StaleDataError) as exc:
                # Defense-in-depth: if a DB constraint fires despite the self-ref
                # filter in _persist_analysis (e.g. a concurrent rename made the
                # target's normalized_name collide with a competitor), roll back
                # and skip this company rather than aborting the whole stage.
                await session.rollback()
                summary.llm_failures += 1
                logger.warning(
                    "DB error persisting competitors for %s — skipping. Error: %s",
                    company_name,
                    exc,
                )
                continue

    if stop:
        # Recorded once total (not per task): the serial loop incremented this
        # exactly once before breaking, and the summary field is a "we stopped
        # for a rate limit" signal, not a per-company tally.
        summary.skipped_rate_limited = 1

    return summary
