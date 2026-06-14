"""resolve-homepages pipeline stage.

For each company that has no website (and was never attempted, or whose
attempt is older than the refetch window), try CANDIDATE_TLDS to find a live
homepage and record the result. Companies that already have a website — e.g.
provided by a VC portfolio adapter — are never re-resolved: doing so wastes
~13s of wall clock per company across the whole table and risks overwriting
a correct discovery-provided URL with a wrong TLD guess.

Commit cadence: one commit per company so a mid-run crash leaves a clean
state, and so a ``max_runtime_minutes`` budget can stop the loop at any
company boundary — the next run's selection naturally resumes where this one
stopped (website_resolved_at IS NULL ⇒ not yet attempted).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from nous.db.models import Company
from nous.sources.homepage import HomepageClient, resolve_homepage
from nous.util.slugify import slugify
from nous.util.url import is_storable_website

logger = logging.getLogger(__name__)

# How many companies to resolve over the network at once. Homepage resolution
# is network-bound (several TLD probes + an optional DuckDuckGo fallback per
# company, ~13s of wall clock), and distinct companies use distinct domains, so
# a batch fetches concurrently without violating the 1 req/sec/domain budget
# (the client's per-domain locks still serialize same-domain requests).
_DEFAULT_CONCURRENCY: int = 8


class ResolveSummary(BaseModel):
    companies_seen: int = 0
    websites_resolved: int = 0
    websites_unchanged: int = 0
    no_match: int = 0
    # Subset of no_match recorded while DDG's circuit breaker was open: only
    # the TLD heuristic ran. These verdicts are weaker — re-attempt early via
    # --refetch-after-days if this number is large.
    no_match_ddg_blocked: int = 0
    errors: int = 0
    # True when the max_runtime_minutes budget stopped the loop before the
    # selection was drained. The remaining companies stay eligible next run.
    stopped_early: bool = False


class _ResolveOutcome(NamedTuple):
    """HTTP-only result of resolving one company (no DB access).

    ``error`` marks companies to count as errors and skip *without* stamping
    website_resolved_at (empty slug or an unexpected failure), so they stay
    eligible next run — matching the original per-company ``continue`` paths.
    """

    resolved: str | None
    ddg_blocked: bool
    error: bool


async def _resolve_one(client: HomepageClient, company: Company) -> _ResolveOutcome:
    """Resolve one company's homepage over the network. No DB access, so this
    is safe to run concurrently for a batch of companies against the shared
    client — its per-domain locks preserve the 1 req/sec/domain budget."""
    slug_base = slugify(company.name)
    # Protect against companies whose slugified name is empty (edge case).
    if not slug_base:
        logger.warning("Company %s has empty slugified name — skipping resolve", company.id)
        return _ResolveOutcome(resolved=None, ddg_blocked=False, error=True)

    try:
        resolved = await resolve_homepage(
            client,
            slug_base=slug_base,
            company_name=company.name,
            rejected_urls=company.rejected_urls or (),
        )
    except Exception:
        logger.exception("Unexpected error resolving homepage for %s", company.name)
        return _ResolveOutcome(resolved=None, ddg_blocked=False, error=True)

    return _ResolveOutcome(resolved=resolved, ddg_blocked=client.ddg_blocked, error=False)


async def run_resolve_homepages(
    session: AsyncSession,
    client: HomepageClient,
    *,
    refetch_after_days: int = 90,
    limit: int | None = None,
    max_runtime_minutes: float | None = None,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> ResolveSummary:
    """For each company with no website whose last attempt is absent or stale,
    attempt to resolve a homepage via common TLDs (+ DDG fallback).

    On a non-None result, update company.website + company.website_resolved_at.
    On None, still set website_resolved_at (so we don't retry every run);
    leave website as-is.

    ``max_runtime_minutes`` is a clean-exit wall-clock budget: the loop stops
    at the next *batch* boundary once exceeded. Combined with per-company
    commits this makes the stage resumable across bounded CI runs.

    ``concurrency`` controls how many companies are resolved over the network
    at once. Resolution is network-bound, so fetching a batch concurrently
    collapses wall-clock roughly ``concurrency``-fold. Only the HTTP work is
    parallelized — DB writes stay strictly sequential on the single passed-in
    session (one connection, one commit per company), so there is no added DB
    concurrency and the existing crash-safety/idempotency is unchanged.
    """
    summary = ResolveSummary()
    started = time.monotonic()
    deadline = (
        started + max_runtime_minutes * 60 if max_runtime_minutes is not None else None
    )

    cutoff = datetime.now(tz=UTC) - timedelta(days=refetch_after_days)

    stmt = select(Company).where(
        Company.website.is_(None),
        Company.exclusion_reason.is_(None),
        or_(
            Company.website_resolved_at.is_(None),
            Company.website_resolved_at < cutoff,
        ),
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    companies = list(result.scalars().all())

    batch_size = max(1, concurrency)
    for start in range(0, len(companies), batch_size):
        if deadline is not None and time.monotonic() >= deadline:
            summary.stopped_early = True
            logger.info(
                "resolve: %.0f-minute budget reached after %d companies — "
                "stopping cleanly (%d left for the next run)",
                max_runtime_minutes,
                summary.companies_seen,
                len(companies) - summary.companies_seen,
            )
            break

        batch = companies[start : start + batch_size]
        # Phase 1: resolve the whole batch concurrently (network only).
        outcomes = await asyncio.gather(*(_resolve_one(client, c) for c in batch))

        # Phase 2: apply results sequentially on the single session, one commit
        # per company — crash-safe + resumable, exactly as the serial version.
        for company, outcome in zip(batch, outcomes, strict=True):
            summary.companies_seen += 1

            if outcome.error:
                summary.errors += 1
                continue

            now = datetime.now(tz=UTC)
            if outcome.resolved is not None:
                if company.website != outcome.resolved:
                    if is_storable_website(outcome.resolved):
                        company.website = outcome.resolved
                        summary.websites_resolved += 1
                    # else: resolve_homepage only ever returns http(s) URLs, so a
                    # non-storable value here is unreachable; never persist it.
                else:
                    # website was already set to this URL
                    summary.websites_unchanged += 1
            else:
                summary.no_match += 1
                if outcome.ddg_blocked:
                    summary.no_match_ddg_blocked += 1
                # website stays as-is; we record the attempt timestamp either way.

            company.website_resolved_at = now
            session.add(company)
            try:
                await session.commit()
            except StaleDataError:
                # The row was deleted out from under us mid-run — almost always
                # a concurrent dedup-companies merge folding this company into
                # another. Roll back and move on instead of crashing.
                await session.rollback()
                logger.warning(
                    "Company %s disappeared mid-resolve (likely a concurrent merge)"
                    " — skipping.",
                    company.id,
                )
                summary.errors += 1
                continue

    return summary
