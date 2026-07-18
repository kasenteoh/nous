"""audit-round-entities — $0 probe: which stored rounds fail entity checks?

The measure-first stage of the entity-aware ingestion arc (BACKLOG 2026-07-17
P0). ``article_mentions_company`` passes same-name different-entity rounds BY
CONSTRUCTION, so before building the ingest-time guard we measure prevalence:
for every shown company's round, does the round's own coverage text
corroborate THIS company as its subject?

Per round, the text is assembled from its linked articles (the 0044 FK plus
the ``primary_news_url`` row): a publisher row's ``raw_content`` is real body
text; a Google-News-host row's is only headline+snippet (mirrors
repair-misattributed-news semantics). Signals come from
:mod:`nous.util.entity_corroboration`, evaluated over the same calibrated
name-variant set the retroactive purge trusts (full name, squashed,
distinctive head token) — the BEST-corroborating variant wins, so "Genesis
raises $200M" never false-flags Genesis Therapeutics. Verdicts:

- ``suspect``  — a signal fired: lowercase-only usage, a consistently
  extended entity phrase ("Primary Wave"), headline-kind occurrences that are
  ALL extended, zero context overlap with at most one bare mention, or the
  name absent from every variant's view of the text;
- ``corroborated`` — at least one bare proper-noun occurrence and no signal;
- ``unknown``  — no stored text at all (coverage gap, never a verdict).

Read-only, deterministic, $0 — prints a JSON report (counts + the suspect
list sorted by amount, capped) for the ops.yml step summary. The suspect list
is the candidate set for LLM adjudication / the retroactive audit; nothing is
ever deleted here.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle

# Private-name imports match repair_misattributed_news.py (the sibling probe)
# — duplicating the curated common-word list here would let the two drift.
from nous.sources.news import _COMMON_NAME_WORDS, _GOOGLE_NEWS_HOST
from nous.util.entity_corroboration import CorroborationResult, corroborate_entity
from nous.util.slugify import strip_corporate_suffix
from nous.util.url import hostname

logger = logging.getLogger(__name__)

# Cap the itemized suspect list in the JSON report (counts are never capped).
SUSPECT_LIMIT: int = 60


class SuspectRound(BaseModel):
    slug: str
    round_type: str | None = None
    amount: str | None = None
    announced_date: str | None = None
    text_kind: str  # "body" | "headline"
    reasons: list[str]
    evidence: list[str] = Field(default_factory=list)
    article_title: str | None = None
    round_id: str


class AuditRoundEntitiesSummary(BaseModel):
    rounds_total: int = 0
    rounds_checked: int = 0
    corroborated: int = 0
    # Split of `corroborated` that sizes the future ingest guard's LLM load:
    # strong = a bare proper-noun mention AND description-context overlap
    # (attach without adjudication); weak = no signal fired but no positive
    # evidence either — the food-Wonder blind spot lives here, and these are
    # exactly the attachments the guard would send to LLM adjudication.
    corroborated_strong: int = 0
    corroborated_weak: int = 0
    suspect: int = 0
    unknown_no_text: int = 0
    body_texts: int = 0
    headline_texts: int = 0
    reason_counts: dict[str, int] = Field(default_factory=dict)
    suspects: list[SuspectRound] = Field(default_factory=list)
    suspects_truncated: int = 0


def _name_variants(company_name: str) -> list[str]:
    """The calibrated variant ladder repair-misattributed-news trusts: full
    name, squashed multi-token ("PhysicsWallah"), distinctive head token
    ("Genesis" for Genesis Therapeutics — >=4 chars, not a common word)."""
    variants = [company_name]
    tokens = strip_corporate_suffix(company_name).lower().split()
    if len(tokens) >= 2:
        variants.append("".join(tokens))
        head = tokens[0]
        if len(head) >= 4 and head not in _COMMON_NAME_WORDS:
            variants.append(head)
    return variants


def _best_corroboration(
    company_name: str, description: str | None, text: str
) -> CorroborationResult:
    """Evaluate every name variant; the BEST-corroborating one wins.

    Only variants that actually OCCUR in the text get a vote — a variant with
    zero occurrences has no evidence in either direction and must not "clear"
    a round (the squashed variant of "Wave Probe" never appears anywhere).
    Among occurring variants: non-suspect preferred (false-keep bias — the
    head-token "Genesis" corroborating spares a "Genesis raises $200M"
    headline even though the full name is absent), then most proper-noun
    occurrences. When NO variant occurs, the full-name view is returned and
    the caller reports the name as absent.
    """
    results = [
        corroborate_entity(v, description, text)
        for v in _name_variants(company_name)
    ]
    eligible = [r for r in results if r.occurrences > 0]
    if not eligible:
        return results[0]
    return max(
        eligible, key=lambda r: (not r.suspect, r.proper_occurrences, r.occurrences)
    )


def _round_text(articles: list[NewsArticle]) -> tuple[str, str]:
    """(text, kind) for a round's article set. Publisher body text wins over
    headline+snippet rows; all titles are always included (headlines name the
    funded company — the highest-signal line we have)."""
    titles = [a.title for a in articles if a.title]
    bodies = [
        a.raw_content
        for a in articles
        if a.raw_content and hostname(a.url) != _GOOGLE_NEWS_HOST
    ]
    if bodies:
        return " ".join((*titles, *bodies)), "body"
    snippets = [
        a.raw_content
        for a in articles
        if a.raw_content and hostname(a.url) == _GOOGLE_NEWS_HOST
    ]
    return " ".join((*titles, *snippets)), "headline"


async def run_audit_round_entities(
    session: AsyncSession,
    *,
    min_amount: Decimal | None = None,
) -> AuditRoundEntitiesSummary:
    """Audit every shown company's rounds for entity corroboration. See
    module docstring. Read-only."""
    rows = (
        await session.execute(
            select(FundingRound, Company.slug, Company.name, Company.description_short)
            .join(Company, FundingRound.company_id == Company.id)
            .where(Company.exclusion_reason.is_(None))
        )
    ).all()
    if min_amount is not None:
        rows = [
            r
            for r in rows
            if r[0].amount_raised is not None and r[0].amount_raised >= min_amount
        ]

    summary = AuditRoundEntitiesSummary(rounds_total=len(rows))
    if not rows:
        return summary

    # Batch-load every round's article set in two queries (round-linked via
    # the 0044 FK; primary_news_url rows), then group in Python — the round
    # table is ~3-4k rows, far below anything needing pagination.
    round_ids = [r[0].id for r in rows]
    primary_urls = {r[0].primary_news_url for r in rows if r[0].primary_news_url}
    by_round: dict[UUID, list[NewsArticle]] = {}
    by_url: dict[str, list[NewsArticle]] = {}
    linked = (
        (
            await session.execute(
                select(NewsArticle).where(NewsArticle.funding_round_id.in_(round_ids))
            )
        )
        .scalars()
        .all()
    )
    for a in linked:
        if a.funding_round_id is not None:
            by_round.setdefault(a.funding_round_id, []).append(a)
    if primary_urls:
        primaries = (
            (
                await session.execute(
                    select(NewsArticle).where(NewsArticle.url.in_(primary_urls))
                )
            )
            .scalars()
            .all()
        )
        for a in primaries:
            by_url.setdefault(a.url, []).append(a)

    all_suspects: list[tuple[Decimal, SuspectRound]] = []
    for round_row, slug, name, description in rows:
        articles: dict[UUID, NewsArticle] = {
            a.id: a for a in by_round.get(round_row.id, [])
        }
        if round_row.primary_news_url:
            for a in by_url.get(round_row.primary_news_url, []):
                # Only this company's own row: a primary URL shared across
                # companies must not leak another company's copy in.
                if a.company_id == round_row.company_id:
                    articles[a.id] = a
        text, kind = _round_text(list(articles.values()))
        if not text.strip():
            summary.unknown_no_text += 1
            continue

        summary.rounds_checked += 1
        if kind == "body":
            summary.body_texts += 1
        else:
            summary.headline_texts += 1

        result = _best_corroboration(name, description, text)
        reasons = list(result.reasons)
        if result.occurrences == 0:
            reasons.append("name absent from stored coverage text")
        elif (
            kind == "headline"
            and not result.suspect
            and result.proper_occurrences > 0
            and result.extended_occurrences == result.proper_occurrences
        ):
            # Headline-only text can't repeat a phrase, so the consistency
            # bar never fires there — but a headline names the funded
            # company, so EVERY proper occurrence being extended ("Impulse
            # Dynamics Raises $136M") is the wrong-entity shape.
            reasons.append("every headline occurrence extends the name")

        if reasons:
            summary.suspect += 1
            for reason in reasons:
                summary.reason_counts[reason] = (
                    summary.reason_counts.get(reason, 0) + 1
                )
            first_title = next(
                (a.title for a in articles.values() if a.title), None
            )
            all_suspects.append(
                (
                    round_row.amount_raised or Decimal(0),
                    SuspectRound(
                        slug=slug,
                        round_type=round_row.round_type,
                        amount=(
                            f"${round_row.amount_raised:,.0f}"
                            if round_row.amount_raised is not None
                            else None
                        ),
                        announced_date=(
                            round_row.announced_date.isoformat()
                            if round_row.announced_date
                            else None
                        ),
                        text_kind=kind,
                        reasons=reasons,
                        evidence=result.evidence,
                        article_title=(
                            first_title[:110] if first_title else None
                        ),
                        round_id=str(round_row.id),
                    ),
                )
            )
        else:
            summary.corroborated += 1
            bare = result.proper_occurrences - result.extended_occurrences
            if bare >= 1 and result.context_overlap >= 1:
                summary.corroborated_strong += 1
            else:
                summary.corroborated_weak += 1

    # Secondary slug key: equal-amount (incl. null-amount) suspects keep a
    # stable order across runs regardless of DB iteration order.
    all_suspects.sort(key=lambda t: (-t[0], t[1].slug))
    summary.suspects = [s for _, s in all_suspects[:SUSPECT_LIMIT]]
    summary.suspects_truncated = max(0, len(all_suspects) - SUSPECT_LIMIT)

    logger.info(
        "audit-round-entities: %d rounds, %d checked (%d body / %d headline), "
        "%d corroborated, %d suspect, %d no-text",
        summary.rounds_total,
        summary.rounds_checked,
        summary.body_texts,
        summary.headline_texts,
        summary.corroborated,
        summary.suspect,
        summary.unknown_no_text,
    )
    return summary
