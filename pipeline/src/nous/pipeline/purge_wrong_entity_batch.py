"""purge-wrong-entity-batch — the retroactive entity audit, as one bounded lever.

The entity-resolution arc (BACKLOG 2026-07-17 P0) shipped its three pieces
separately: the $0 probe (``audit-round-entities``, ~213 suspect rounds on
prod), the ingest guard (#235), and the per-company purge
(``purge-wrong-entity-articles``). What remained was the retroactive pass the
probe exists to feed — "dispatch the purge per suspect, review, apply" — which
by hand is ~150 single-company ops dispatches. This stage walks that queue:

1. run the probe with the itemized list UNCAPPED;
2. queue each suspect company once, ordered by its largest suspect round
   (the marquee wrong-entity money first — built ← Built In $30B,
   blue ← Blue Origin $10B);
3. run the SAME per-company purge over each, in its own session (one wedged
   company never poisons the next), with the hold rail on.

Safety:

- **Dry-run by default.** The report lists every would-purge article title
  and the other entity the adjudicator named, per company, for review.
- **Hold rail.** A company whose coverage is (nearly) all another entity's
  is HELD, never purged: that shape means the profile itself is the wrong
  entity (an exclude / reresolve decision for a human), and an article sweep
  would strip it to a husk while leaving the wrong identity in place.
- **Fail-KEEP** per article and **stop-on-429** for the whole queue are
  inherited from the per-company lever; a company it refuses (no description
  to adjudicate against) is reported as skipped.
- **Bounded.** ``limit`` companies per dispatch plus a wall-clock budget
  checked at company boundaries; ``offset`` pages deeper into the queue.
  Re-running is idempotent: purged rounds leave the probe's suspect set, and
  a company whose articles all adjudicate as ours is simply re-confirmed.
- **Skip list.** ``skip`` slugs are reported but never adjudicated. The
  intended apply flow is: dry-run a page, review it, then apply that same page
  with ``skip`` naming every company whose PROFILE looks like the wrong entity.
  The first prod dry-run (2026-10-09) proved the need: ``prometheus`` (a
  prometheus.com profile carrying Bezos's $12B Prometheus coverage) adjudicated
  58% "another entity" — under the hold rail, yet purging it would delete the
  real story the slug is named for. The fix there is a website re-resolve, not
  an article sweep.

Cost: one DeepSeek adjudication per stored article of each queued company
(~$0.0005 each; a few dozen articles per company), so ~$0.01 per company.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nous.pipeline.audit_round_entities import run_audit_round_entities
from nous.pipeline.purge_wrong_entity_articles import (
    PurgeRateLimitedError,
    PurgeWrongEntityError,
    run_purge_wrong_entity_articles,
)

logger = logging.getLogger(__name__)

# Defaults for the hold rail: >= 80% of >= 3 checked articles adjudicated as
# another entity means the profile, not the coverage, is wrong.
DEFAULT_HOLD_FRACTION: float = 0.8
HOLD_MIN_ARTICLES: int = 3
# Would-purge titles itemized per company in the report (counts are exact).
_TITLES_PER_COMPANY: int = 12

Outcome = Literal["purged", "would_purge", "clean", "held", "skipped", "operator_skip"]


class BatchCompanyResult(BaseModel):
    slug: str
    largest_suspect_amount: str | None = None
    suspect_rounds: int = 0
    outcome: Outcome
    articles_checked: int = 0
    articles_purged: int = 0
    articles_llm_error_kept: int = 0
    rounds_purged: int = 0
    round_labels: list[str] = Field(default_factory=list)
    total_raised_cleared: bool = False
    status_reset: bool = False
    # "title — other entity" for each article adjudicated as NOT this company.
    purged_titles: list[str] = Field(default_factory=list)
    note: str | None = None


class PurgeWrongEntityBatchSummary(BaseModel):
    dry_run: bool = True
    suspect_rounds_total: int = 0
    companies_in_queue: int = 0
    offset: int = 0
    companies_processed: int = 0
    companies_purged: int = 0
    companies_clean: int = 0
    companies_held: int = 0
    companies_skipped: int = 0
    companies_operator_skipped: int = 0
    articles_checked: int = 0
    articles_purged: int = 0
    rounds_purged: int = 0
    stopped_early: bool = False
    aborted_rate_limited: bool = False
    results: list[BatchCompanyResult] = Field(default_factory=list)


def build_queue(
    suspect_slugs_by_amount: list[tuple[str, str | None]],
) -> list[tuple[str, str | None, int]]:
    """Collapse the probe's amount-sorted suspect rounds to one entry per
    company, keeping first-appearance order (= largest suspect round first).

    Returns ``(slug, largest_amount_label, suspect_round_count)``. Pure.
    """
    order: list[str] = []
    largest: dict[str, str | None] = {}
    counts: dict[str, int] = {}
    for slug, amount in suspect_slugs_by_amount:
        if slug not in counts:
            order.append(slug)
            largest[slug] = amount
            counts[slug] = 0
        counts[slug] += 1
    return [(slug, largest[slug], counts[slug]) for slug in order]


async def run_purge_wrong_entity_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: int = 10,
    offset: int = 0,
    min_amount: Decimal | None = None,
    dry_run: bool = True,
    hold_fraction: float = DEFAULT_HOLD_FRACTION,
    max_runtime_minutes: float | None = None,
    skip: frozenset[str] = frozenset(),
) -> PurgeWrongEntityBatchSummary:
    """Probe for suspect rounds, then purge-adjudicate the top suspect
    companies. See module doc."""
    summary = PurgeWrongEntityBatchSummary(dry_run=dry_run, offset=offset)
    async with session_factory() as session:
        audit = await run_audit_round_entities(
            session, min_amount=min_amount, suspect_limit=None
        )
    summary.suspect_rounds_total = len(audit.suspects)
    queue = build_queue([(s.slug, s.amount) for s in audit.suspects])
    summary.companies_in_queue = len(queue)

    deadline = (
        time.monotonic() + max_runtime_minutes * 60
        if max_runtime_minutes is not None
        else None
    )
    for slug, largest, n_rounds in queue[offset : offset + limit]:
        if deadline is not None and time.monotonic() >= deadline:
            summary.stopped_early = True
            break
        result = BatchCompanyResult(
            slug=slug,
            largest_suspect_amount=largest,
            suspect_rounds=n_rounds,
            outcome="skipped",
        )
        summary.results.append(result)
        summary.companies_processed += 1
        if slug in skip:
            # Keeps its queue position so --offset paging is unchanged.
            result.outcome = "operator_skip"
            result.note = "skipped by operator (--skip)"
            summary.companies_operator_skipped += 1
            continue
        try:
            async with session_factory() as session:
                purge = await run_purge_wrong_entity_articles(
                    session,
                    slug=slug,
                    force_adjudicate=True,
                    dry_run=dry_run,
                    hold_fraction=hold_fraction,
                    hold_min_articles=HOLD_MIN_ARTICLES,
                )
        except PurgeRateLimitedError as exc:
            result.note = str(exc)
            summary.companies_skipped += 1
            summary.aborted_rate_limited = True
            logger.warning("purge-wrong-entity-batch: rate-limited at %s — stopping", slug)
            break
        except PurgeWrongEntityError as exc:
            # No description to adjudicate against, or the company vanished
            # (merged away) between the probe and its turn.
            result.note = str(exc)
            summary.companies_skipped += 1
            continue

        result.articles_checked = purge.articles_checked
        result.articles_purged = purge.articles_purged
        result.articles_llm_error_kept = purge.articles_llm_error_kept
        result.rounds_purged = purge.rounds_purged
        result.round_labels = purge.round_labels
        result.total_raised_cleared = purge.total_raised_cleared
        result.status_reset = purge.status_reset
        result.purged_titles = [
            f"{v.title} — {v.other_entity or 'other entity'}"
            for v in purge.verdicts
            if not v.keep
        ][:_TITLES_PER_COMPANY]
        summary.articles_checked += purge.articles_checked

        if purge.held:
            result.outcome = "held"
            result.note = purge.hold_reason
            summary.companies_held += 1
        elif purge.articles_purged == 0:
            result.outcome = "clean"
            summary.companies_clean += 1
        else:
            result.outcome = "would_purge" if dry_run else "purged"
            summary.companies_purged += 1
            summary.articles_purged += purge.articles_purged
            summary.rounds_purged += purge.rounds_purged

    logger.info(
        "purge-wrong-entity-batch%s: queue=%d offset=%d processed=%d "
        "purged=%d clean=%d held=%d skipped=%d articles=%d/%d rounds=%d",
        " (dry-run)" if dry_run else "",
        summary.companies_in_queue,
        offset,
        summary.companies_processed,
        summary.companies_purged,
        summary.companies_clean,
        summary.companies_held,
        summary.companies_skipped,
        summary.articles_purged,
        summary.articles_checked,
        summary.rounds_purged,
    )
    return summary


def render_batch_table(summary: PurgeWrongEntityBatchSummary) -> str:
    """Markdown step-summary table: one row per processed company."""
    mode = "DRY-RUN" if summary.dry_run else "APPLIED"
    lines = [
        f"\n### purge-wrong-entity-batch — {mode}\n",
        f"Queue {summary.companies_in_queue} companies "
        f"({summary.suspect_rounds_total} suspect rounds); processed "
        f"{summary.companies_processed} from offset {summary.offset}: "
        f"{summary.companies_purged} purge, {summary.companies_clean} clean, "
        f"{summary.companies_held} held, {summary.companies_skipped} skipped, "
        f"{summary.companies_operator_skipped} operator-skipped"
        + (" — **stopped: rate-limited**" if summary.aborted_rate_limited else "")
        + (" — stopped: runtime budget" if summary.stopped_early else "")
        + "\n",
        "| company | largest suspect | outcome | articles purged | rounds purged | note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in summary.results:
        note = (r.note or "; ".join(r.round_labels) or "").replace("|", "/")
        lines.append(
            f"| `{r.slug}` | {r.largest_suspect_amount or '—'} | {r.outcome} | "
            f"{r.articles_purged}/{r.articles_checked} | {r.rounds_purged} | "
            f"{note[:160]} |"
        )
    return "\n".join(lines) + "\n\n"
