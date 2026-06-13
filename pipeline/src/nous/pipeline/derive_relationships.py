"""derive-relationships pipeline stage.

Populates the ``company_relationships`` graph from data we already hold — zero
LLM cost — and is fully idempotent (replace-style per ``source``):

1. **competitor** edges — projected set-based from resolved ``competitors`` rows
   (those whose ``competitor_company_id`` is non-NULL, i.e. the named competitor
   is a company in our DB). ``score = 1/rank`` so rank-1 competitors sort first;
   ``evidence`` carries the competitor's reasoning. Deduplicated to one row per
   ``(company_id, competitor_company_id)`` pair (the competitors table is unique
   on ``(company_id, rank)``, so a company can list the same competitor twice at
   different ranks — keep the best/lowest rank).

2. **similar** edges — computed in Python from shared ``industry_group`` + tag
   overlap. Within each industry_group, a company's peers are scored
   ``2 * |shared tags| + (1 if same primary_category)``; peers scoring < 1 are
   dropped (so a coarse industry alone never links everything indiscriminately),
   and only the top ``max_similar_per_company`` are kept. Directed, written for
   both endpoints (symmetric relatedness), bounded at K*N rows. Ties broken
   deterministically so the same run yields the same edge set (stable output).

"Also backed by" (shared-investor) edges are deliberately NOT materialized here:
a mega-investor makes them O(N^2). They are derived at read time in the web
layer, capped, with high-degree investors excluded.

Idempotency: each ``source`` is replaced wholesale — DELETE the source's rows,
then INSERT the freshly derived set, all in one transaction (one commit), so the
graph is never left half-built. Runs weekly in discovery.yml right after
``analyze-competitors`` -> ``link-competitors`` (which resolves more competitor
FKs, densifying the projection).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanyRelationship, Competitor

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SIMILAR_PER_COMPANY: int = 8

# The six columns derive-relationships writes; id/created_at/updated_at fall back
# to their DB server defaults (gen_random_uuid()/now()).
_INSERT_COLUMNS: list[str] = [
    "company_id",
    "related_company_id",
    "relationship_type",
    "score",
    "source",
    "evidence",
]


class DeriveRelationshipsSummary(BaseModel):
    competitor_edges: int = 0
    similar_edges: int = 0


def _compute_similar_edges(
    rows: Sequence[tuple[UUID, str | None, str | None, list[str] | None]],
    *,
    max_per_company: int,
) -> list[dict[str, Any]]:
    """Compute directed 'similar' edges from industry_group + tag overlap.

    ``rows`` are ``(id, industry_group, primary_category, tags)`` for companies
    with a non-NULL industry_group. Returns insert-ready dicts.
    """
    groups: dict[str, list[tuple[UUID, str | None, frozenset[str]]]] = defaultdict(list)
    for cid, industry_group, category, tags in rows:
        if industry_group is None:
            continue
        groups[industry_group].append((cid, category, frozenset(tags or ())))

    edges: list[dict[str, Any]] = []
    for industry_group, members in groups.items():
        if len(members) < 2:
            continue
        for cid, category, tagset in members:
            scored: list[tuple[int, int, str, UUID]] = []
            for other_id, other_category, other_tags in members:
                if other_id == cid:
                    continue
                shared = len(tagset & other_tags)
                same_category = bool(
                    category and other_category and category == other_category
                )
                score = 2 * shared + (1 if same_category else 0)
                if score < 1:
                    continue
                # str(other_id) is the deterministic final tiebreak → stable output.
                scored.append((score, shared, str(other_id), other_id))
            # Highest score first, then most shared tags, then a stable id order.
            scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
            for score, shared, _id_str, other_id in scored[:max_per_company]:
                suffix = (
                    f"; {shared} shared tag{'s' if shared != 1 else ''}"
                    if shared
                    else ""
                )
                edges.append(
                    {
                        "company_id": cid,
                        "related_company_id": other_id,
                        "relationship_type": "similar",
                        "score": Decimal(score),
                        "source": "industry_tags",
                        "evidence": f"Both in {industry_group}{suffix}",
                    }
                )
    return edges


async def run_derive_relationships(
    session: AsyncSession,
    *,
    dry_run: bool = False,
    max_similar_per_company: int = _DEFAULT_MAX_SIMILAR_PER_COMPANY,
) -> DeriveRelationshipsSummary:
    """Rebuild the company_relationships graph from competitors + industry/tags.

    Replace-style and idempotent: each ``source`` is deleted and re-derived in a
    single transaction. ``dry_run`` computes and reports the would-be edge counts
    without writing.
    """
    summary = DeriveRelationshipsSummary()

    # SELECT projecting resolved competitor rows into edge tuples. DISTINCT ON
    # (company_id, competitor_company_id) keeps one row per pair — lowest rank
    # (strongest) wins — so the set-based INSERT can't violate the unique triple.
    # Self-references are excluded (the target table CHECK forbids them).
    competitor_select = (
        select(
            Competitor.company_id,
            Competitor.competitor_company_id,
            literal("competitor").label("relationship_type"),
            (literal(1.0) / func.greatest(Competitor.rank, literal(1))).label("score"),
            literal("competitors").label("source"),
            Competitor.reasoning.label("evidence"),
        )
        .where(Competitor.competitor_company_id.is_not(None))
        .where(Competitor.competitor_company_id != Competitor.company_id)
        .distinct(Competitor.company_id, Competitor.competitor_company_id)
        .order_by(
            Competitor.company_id,
            Competitor.competitor_company_id,
            Competitor.rank.asc(),
        )
    )
    competitor_count = (
        await session.execute(
            select(func.count()).select_from(competitor_select.subquery())
        )
    ).scalar_one()
    summary.competitor_edges = int(competitor_count)

    # Compute similar edges in Python (a read + in-memory scoring).
    rows = (
        await session.execute(
            select(
                Company.id,
                Company.industry_group,
                Company.primary_category,
                Company.tags,
            ).where(Company.industry_group.is_not(None))
        )
    ).all()
    similar_edges = _compute_similar_edges(
        [(r[0], r[1], r[2], r[3]) for r in rows],
        max_per_company=max_similar_per_company,
    )
    summary.similar_edges = len(similar_edges)

    if dry_run:
        logger.info(
            "derive-relationships (dry-run): %d competitor + %d similar edges",
            summary.competitor_edges,
            summary.similar_edges,
        )
        return summary

    # Replace each source wholesale in one transaction (one commit) so the graph
    # is never left half-built.
    await session.execute(
        delete(CompanyRelationship).where(CompanyRelationship.source == "competitors")
    )
    await session.execute(
        insert(CompanyRelationship).from_select(_INSERT_COLUMNS, competitor_select)
    )
    await session.execute(
        delete(CompanyRelationship).where(
            CompanyRelationship.source == "industry_tags"
        )
    )
    if similar_edges:
        await session.execute(insert(CompanyRelationship), similar_edges)

    await session.commit()
    logger.info(
        "derive-relationships: wrote %d competitor + %d similar edges",
        summary.competitor_edges,
        summary.similar_edges,
    )
    return summary
