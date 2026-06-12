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

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Competitor, NewsArticle
from nous.llm.client import LLMError, LLMParseError, LLMRateLimitError, complete_json
from nous.llm.prompts.competitor_analysis import (
    MAX_PEERS,
    CompetitorAnalysis,
    Peer,
    Target,
    build_prompt,
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
# Main loop
# ---------------------------------------------------------------------------


async def run_analyze_competitors(
    session: AsyncSession,
    *,
    limit: int = 500,
    ttl_days: int = 25,
    dry_run: bool = False,
) -> AnalyzeCompetitorsSummary:
    summary = AnalyzeCompetitorsSummary()

    companies = await fetch_eligible_companies(
        session, limit=limit, ttl_days=ttl_days
    )

    for company in companies:
        # --- Pass 1: pull competitor candidates from the company's TC coverage.
        # candidate_map: normalized competitor name -> source TechCrunch URL.
        tc_articles = await fetch_techcrunch_articles(session, company_id=company.id)
        candidate_map: dict[str, str] = {}
        candidate_names: list[str] = []
        if tc_articles:
            cand_prompt = build_candidates_prompt(
                target_name=company.name, articles=tc_articles
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
                summary.skipped_rate_limited += 1
                break
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
        peers = await fetch_peers(session, target=company)
        target = Target(
            name=company.name,
            description_short=company.description_short or "",
            description_long=company.description_long or "",
            industry_group=company.industry_group or "",
        )
        prompt = build_prompt(target=target, peers=peers, tc_candidates=candidate_names)

        try:
            analysis: CompetitorAnalysis = await complete_json(
                prompt, CompetitorAnalysis
            )
        except LLMRateLimitError:
            logger.warning(
                "LLM rate limit hit while analyzing competitors for %s — "
                "stopping loop to avoid further quota exhaustion.",
                company.name,
            )
            summary.skipped_rate_limited += 1
            break
        except (LLMParseError, LLMError) as exc:
            logger.warning(
                "LLM error analyzing competitors for %s: %s", company.name, exc
            )
            summary.llm_failures += 1
            continue

        summary.companies_analyzed += 1

        # Resolve each competitor name to a company_id (None if unmatched) and
        # determine provenance: a competitor whose normalized name came from the
        # TechCrunch candidates is sourced to that article; everything else the
        # model added is "llm_inferred" (rendered as a *potential* competitor).
        # Resolve each competitor and collect in LLM order (sorted by rank).
        # The CompetitorAnalysis validator already guarantees contiguous 1..N
        # ranks; this re-rank is belt-and-braces so a future relaxation of that
        # validation can't violate the unique (company_id, rank) constraint.
        llm_ordered = sorted(analysis.competitors, key=lambda x: x.rank)
        resolved: list[tuple[UUID | None, str, str, str, int, str, str | None]] = []
        for contiguous_rank, c in enumerate(llm_ordered, start=1):
            cid = await resolve_competitor_company_id(session, name=c.name)
            article_url = candidate_map.get(normalize_name(c.name))
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
            continue

        # Replace-style write: delete then insert in one transaction. The outer
        # session manages the transaction; we use a SAVEPOINT via begin_nested()
        # so the eligibility loop's prior writes stay intact if this one fails.
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
        await session.flush()

    return summary
