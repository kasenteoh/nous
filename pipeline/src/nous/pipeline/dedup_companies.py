"""dedup-companies pipeline stage.

Collapses duplicate company rows that name-only matching let through. Two
passes:

1. **Exact-domain clustering.** Group companies by ``canonical_domain(website)``
   (shared-hosting domains and websiteless rows are skipped). Any group with
   more than one row is the same company under different names — auto-merge the
   extras into a chosen survivor. No LLM needed: a shared real domain is decisive.

2. **Fuzzy adjudication.** Among the rows left standing, generate candidate
   pairs from soft signals — trigram-similar normalized names, or a shared
   (hq_city, hq_state) with a weaker name similarity — and ask the LLM whether
   each pair is the same company. Merge ONLY on ``same_company=true`` AND
   ``confidence='high'``. Everything else is left alone.

Survivor preference (both passes): prefer the row that already has a
``description_long`` (most-enriched), then one with a ``website``, then the
earliest ``created_at`` (most-established). Ties broken by id for determinism.

Idempotency: merging is a one-way fold (the loser id ceases to exist), so a
second run finds the same domain groups collapsed to one row and the same
fuzzy pairs already merged — nothing new to do. Per-merge commits mean a
partial run leaves a consistent DB.

A merged-away loser's slug is not lost: ``merge_companies`` records it in
``slug_aliases`` so the web layer permanently redirects the dead URL to the
survivor (see the slug_aliases section of its docstring for chain semantics).

Quota discipline (spec §11): at most ``llm_limit`` LLM judgments per run,
highest-similarity pairs first. When more candidates exist than the cap, the
overflow count is logged and reported in the summary — never silently dropped.

``dry_run=True`` performs every read and LLM call but skips the merges/commits,
reporting what *would* be merged.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.db.upsert import merge_companies
from nous.llm.client import LLMError, LLMParseError, LLMRateLimitError, complete_json
from nous.llm.prompts.company_match import CompanyMatch, build_company_match_prompt
from nous.util.url import canonical_domain

logger = logging.getLogger(__name__)

# Trigram thresholds for fuzzy-candidate generation (pass 2).
# NAME_SIMILARITY_THRESHOLD is the floor for a name-only candidate; the lower
# CO_LOCATED_NAME_THRESHOLD applies only when two rows also share an HQ, where
# the location corroborates a weaker name match. Both are deliberately loose —
# they only nominate candidates; the LLM is the actual gate.
NAME_SIMILARITY_THRESHOLD = 0.45
CO_LOCATED_NAME_THRESHOLD = 0.30


class DedupSummary(BaseModel):
    companies_seen: int = 0
    domain_merges: int = 0
    llm_judged: int = 0
    llm_merges: int = 0
    skipped: int = 0


class _CompanyRow(BaseModel):
    """Lightweight projection of a company used for survivor selection + the
    LLM prompt, so we don't hold full ORM objects across per-merge commits."""

    model_config = {"arbitrary_types_allowed": True}

    id: UUID
    name: str
    normalized_name: str
    website: str | None
    hq_city: str | None
    hq_state: str | None
    description_short: str | None
    description_long: str | None
    latest_round_amount: Decimal | None
    latest_round_date: date | None
    latest_round_type: str | None
    created_at: datetime

    def to_prompt_dict(self) -> dict[str, object]:
        # The latest-round denorms are the strongest same-company evidence
        # for website-less husks (bunkerhill + bunkerhill-health both carried
        # one fresh $55M round, but the adjudicator couldn't see it and kept
        # declining the merge — 2026-07-17 QA). Rendered as one line; absent
        # facts are omitted, never guessed.
        funding = None
        if self.latest_round_amount is not None or self.latest_round_date is not None:
            parts = []
            if self.latest_round_type:
                parts.append(self.latest_round_type)
            if self.latest_round_amount is not None:
                parts.append(f"${self.latest_round_amount:,.0f}")
            if self.latest_round_date is not None:
                parts.append(f"announced {self.latest_round_date.isoformat()}")
            funding = " ".join(parts)
        return {
            "name": self.name,
            "website": self.website,
            # Prefer the long description for the adjudicator, fall back to short.
            "description": self.description_long or self.description_short,
            "hq_city": self.hq_city,
            "hq_state": self.hq_state,
            "latest_funding": funding,
        }


def _survivor_sort_key(row: _CompanyRow) -> tuple[int, int, datetime, str]:
    """Sort key whose minimum is the preferred survivor.

    Lower is better: rows with a long description rank ahead of those without;
    then rows with a website; then earlier ``created_at``; id as a final stable
    tiebreak. (Booleans are inverted via ``not`` so True → 0 sorts first.)
    """
    return (
        int(not bool(row.description_long)),
        int(not bool(row.website)),
        row.created_at,
        str(row.id),
    )


def _choose_survivor(rows: list[_CompanyRow]) -> _CompanyRow:
    return min(rows, key=_survivor_sort_key)


async def _load_companies(session: AsyncSession) -> list[_CompanyRow]:
    stmt = select(
        Company.id,
        Company.name,
        Company.normalized_name,
        Company.website,
        Company.hq_city,
        Company.hq_state,
        Company.description_short,
        Company.description_long,
        Company.latest_round_amount,
        Company.latest_round_date,
        Company.latest_round_type,
        Company.created_at,
    )
    result = await session.execute(stmt)
    return [
        _CompanyRow(
            id=r.id,
            name=r.name,
            normalized_name=r.normalized_name,
            website=r.website,
            hq_city=r.hq_city,
            hq_state=r.hq_state,
            description_short=r.description_short,
            description_long=r.description_long,
            latest_round_amount=r.latest_round_amount,
            latest_round_date=r.latest_round_date,
            latest_round_type=r.latest_round_type,
            created_at=r.created_at,
        )
        for r in result
    ]


async def _run_domain_pass(
    session: AsyncSession,
    rows: list[_CompanyRow],
    summary: DedupSummary,
    *,
    dry_run: bool,
) -> set[UUID]:
    """Cluster ``rows`` by canonical domain and merge each multi-row cluster.

    Returns the set of ids that were merged away (losers) so the fuzzy pass can
    exclude them. In ``dry_run`` mode no merge/commit happens but losers are
    still reported so the count reflects what would be collapsed.
    """
    groups: dict[str, list[_CompanyRow]] = {}
    for row in rows:
        domain = canonical_domain(row.website)
        if domain is None:
            continue
        groups.setdefault(domain, []).append(row)

    merged_away: set[UUID] = set()
    for domain, group in groups.items():
        if len(group) < 2:
            continue
        survivor = _choose_survivor(group)
        losers = [r for r in group if r.id != survivor.id]
        for loser in losers:
            merged_away.add(loser.id)
            summary.domain_merges += 1
            if dry_run:
                continue
            await merge_companies(
                session, survivor_id=survivor.id, loser_id=loser.id
            )
        if not dry_run and losers:
            await session.commit()
            logger.info(
                "dedup: domain %s — merged %d row(s) into survivor %s",
                domain,
                len(losers),
                survivor.id,
            )
    return merged_away


async def _generate_fuzzy_pairs(
    session: AsyncSession, candidate_ids: set[UUID]
) -> list[tuple[UUID, UUID, float]]:
    """Return unordered candidate pairs ``(id_a, id_b, similarity)`` among
    ``candidate_ids``, highest similarity first.

    A pair qualifies when either:
    - normalized-name trigram similarity ≥ NAME_SIMILARITY_THRESHOLD, OR
    - the two rows share a non-null (hq_city, hq_state) AND name similarity ≥
      CO_LOCATED_NAME_THRESHOLD.

    The self-join is ordered ``a.id < b.id`` so each pair appears once. The
    pg_trgm GIN index on normalized_name backs ``func.similarity``.
    """
    if len(candidate_ids) < 2:
        return []

    a = Company.__table__.alias("a")
    b = Company.__table__.alias("b")
    similarity = func.similarity(a.c.normalized_name, b.c.normalized_name)

    co_located = and_(
        a.c.hq_city.is_not(None),
        a.c.hq_state.is_not(None),
        func.lower(a.c.hq_city) == func.lower(b.c.hq_city),
        func.lower(a.c.hq_state) == func.lower(b.c.hq_state),
        similarity >= CO_LOCATED_NAME_THRESHOLD,
    )

    stmt = (
        select(a.c.id, b.c.id, similarity.label("sim"))
        .select_from(
            a.join(
                b,
                and_(
                    a.c.id < b.c.id,
                    a.c.id.in_(candidate_ids),
                    b.c.id.in_(candidate_ids),
                    or_(similarity >= NAME_SIMILARITY_THRESHOLD, co_located),
                ),
            )
        )
        .order_by(similarity.desc())
    )
    result = await session.execute(stmt)
    return [(r[0], r[1], float(r.sim)) for r in result]


async def _run_fuzzy_pass(
    session: AsyncSession,
    rows: list[_CompanyRow],
    merged_away: set[UUID],
    summary: DedupSummary,
    *,
    llm_limit: int,
    dry_run: bool,
) -> None:
    """Adjudicate fuzzy candidate pairs with the LLM and merge HIGH-confidence
    matches. Caps LLM judgments at ``llm_limit`` (highest-similarity first)."""
    by_id = {row.id: row for row in rows if row.id not in merged_away}
    candidate_ids = set(by_id)
    pairs = await _generate_fuzzy_pairs(session, candidate_ids)

    if len(pairs) > llm_limit:
        summary.skipped += len(pairs) - llm_limit
        logger.warning(
            "dedup: %d fuzzy candidate pairs exceed llm_limit=%d; judging the "
            "%d highest-similarity pairs and deferring %d to the next run.",
            len(pairs),
            llm_limit,
            llm_limit,
            len(pairs) - llm_limit,
        )
        pairs = pairs[:llm_limit]

    # A row may appear in several pairs; once it's merged away (as survivor or
    # loser) we must not reuse the stale projection. Track the live id set.
    gone: set[UUID] = set()

    for id_a, id_b, _sim in pairs:
        if id_a in gone or id_b in gone:
            continue
        row_a = by_id.get(id_a)
        row_b = by_id.get(id_b)
        if row_a is None or row_b is None:
            continue

        prompt = build_company_match_prompt(
            row_a.to_prompt_dict(), row_b.to_prompt_dict()
        )
        try:
            match: CompanyMatch = await complete_json(prompt, CompanyMatch)
        except LLMRateLimitError:
            logger.warning(
                "dedup: LLM rate limit hit — stopping fuzzy pass to avoid "
                "further quota exhaustion."
            )
            break
        except (LLMParseError, LLMError) as exc:
            logger.warning(
                "dedup: LLM error judging %s vs %s: %s", id_a, id_b, exc
            )
            continue

        summary.llm_judged += 1

        if not (match.same_company and match.confidence == "high"):
            continue

        survivor = _choose_survivor([row_a, row_b])
        loser = row_b if survivor.id == row_a.id else row_a
        summary.llm_merges += 1
        if dry_run:
            continue
        await merge_companies(
            session, survivor_id=survivor.id, loser_id=loser.id
        )
        await session.commit()
        gone.add(loser.id)
        logger.info(
            "dedup: LLM merged %s into %s (sim=%.2f)",
            loser.id,
            survivor.id,
            _sim,
        )


async def run_dedup_companies(
    session: AsyncSession,
    *,
    llm_limit: int = 200,
    dry_run: bool = False,
) -> DedupSummary:
    """Deduplicate companies: exact-domain auto-merge, then LLM-gated fuzzy merge.

    See the module docstring for the algorithm and survivor rule. Returns a
    :class:`DedupSummary` of counts.
    """
    summary = DedupSummary()

    rows = await _load_companies(session)
    summary.companies_seen = len(rows)

    merged_away = await _run_domain_pass(session, rows, summary, dry_run=dry_run)

    # Reload projections after the domain pass so the fuzzy pass sees survivors'
    # inherited description/website/HQ (and not the merged-away losers). In a
    # dry run nothing was merged, so the original snapshot is still accurate.
    if not dry_run and merged_away:
        rows = await _load_companies(session)
        merged_away = set()

    await _run_fuzzy_pass(
        session,
        rows,
        merged_away,
        summary,
        llm_limit=llm_limit,
        dry_run=dry_run,
    )

    return summary
