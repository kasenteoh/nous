"""Ingest-time entity guard — same-name different-entity attachment killer.

The recurrence engine of the 2026-07-17 P0: ``article_mentions_company``
proves the NAME appears in an article, but edtech-Wonder vs food-Wonder both
"say Wonder" — so purged wrong-entity rounds re-ingested within HOURS of the
2026-07-18 delete-round applies (wonder + terrafirma both recurred). This
guard runs at the attachment point and decides whether the article's funded
subject IS the company, in two layers:

1. **Cheap deterministic signals** (:mod:`nous.util.entity_corroboration`,
   calibrated against three live prod probe runs — see #232–#234):
   - STRONG corroboration (a bare proper-noun mention AND description-context
     overlap) → attach, no LLM.
   - No profile to judge against (husk without a description) → attach; the
     retroactive audit owns that cohort — an ingest guard with no evidence
     must not guess.
2. **LLM adjudication** (``article_subject_match``, DeepSeek) for everything
   between — cheap-suspect (extension/lowercase signals fired) and
   cheap-weak (no signal, but no positive evidence either: the food-Wonder
   shape). Attach ONLY on ``is_subject=true`` with confidence better than
   'low'. Run-3 probe sizing: weak+suspect ≈ 75% of headline-only rounds but
   far less of body-bearing fresh ingests; at tens of new articles per cron
   this is cents per day.

Failure semantics: an LLM error SKIPS the article without storing it — the
URL stays absent so the next sweep retries (self-healing, never fail-open
into a wrong attach, never a permanent drop on a transient error).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from nous.db.models import Company
from nous.llm.client import LLMError, LLMRateLimitError, complete_json
from nous.llm.prompts.article_subject_match import (
    ArticleSubjectMatch,
    build_article_subject_match_prompt,
)
from nous.util.entity_corroboration import best_corroboration

logger = logging.getLogger(__name__)


class GuardDecision(BaseModel):
    """One attachment decision, with the reason trail for the run log."""

    attach: bool
    adjudicated: bool = False
    llm_error: bool = False
    # A 429 means every subsequent adjudication this run would also 429 —
    # the caller should stop calling the guard for LLM-requiring articles
    # (skip-unstored, retried next sweep) instead of burning N futile calls.
    rate_limited: bool = False
    reason: str
    other_entity: str | None = None


def _company_hq(company: Company) -> str | None:
    parts = [p for p in (company.hq_city, company.hq_state) if p]
    return ", ".join(parts) if parts else None


async def check_article_entity(
    company: Company, *, title: str, text: str, allow_llm: bool = True
) -> GuardDecision:
    """Decide whether (title, text) is about ``company``. See module doc.

    ``allow_llm=False`` is the caller's rate-limit circuit breaker: cheap
    verdicts (strong-corroboration, no-profile) still attach, but an article
    that would need adjudication skips unstored instead of burning a call.
    """
    if not (company.description_short or "").strip():
        return GuardDecision(attach=True, reason="no-profile")

    combined = text if title.strip() in text else f"{title}. {text}"
    cheap = best_corroboration(
        company.name,
        company.description_short,
        combined,
        own_context=f"{company.website or ''} {company.slug}",
    )
    bare = cheap.proper_occurrences - cheap.extended_occurrences
    if not cheap.suspect and bare >= 1 and cheap.context_overlap >= 1:
        return GuardDecision(attach=True, reason="strong-corroboration")

    if not allow_llm:
        return GuardDecision(
            attach=False, llm_error=True, reason="llm-circuit-open"
        )

    prompt = build_article_subject_match_prompt(
        name=company.name,
        website=company.website,
        description=company.description_short,
        industry=company.industry_group,
        hq=_company_hq(company),
        title=title,
        article_text=text,
    )
    try:
        verdict = await complete_json(prompt, ArticleSubjectMatch)
    except LLMRateLimitError:
        logger.warning(
            "entity-guard rate-limited for %s on %r — the caller should stop "
            "adjudicating for the rest of this run (articles skip unstored "
            "and retry next sweep)",
            company.slug,
            title[:90],
        )
        return GuardDecision(
            attach=False,
            adjudicated=True,
            llm_error=True,
            rate_limited=True,
            reason="llm-rate-limited",
        )
    except LLMError:
        logger.exception(
            "entity-guard adjudication failed for %s on %r — skipping the "
            "article without storing (next sweep retries)",
            company.slug,
            title[:90],
        )
        return GuardDecision(
            attach=False, adjudicated=True, llm_error=True, reason="llm-error"
        )
    if verdict.is_subject and verdict.confidence != "low":
        return GuardDecision(
            attach=True, adjudicated=True, reason=f"llm-match-{verdict.confidence}"
        )
    return GuardDecision(
        attach=False,
        adjudicated=True,
        reason="llm-mismatch",
        other_entity=verdict.other_entity_name,
    )
