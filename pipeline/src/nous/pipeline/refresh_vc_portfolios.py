"""refresh-vc-portfolios pipeline stage.

Iterate the registered VC adapters (``nous.sources.vc_portfolios.ADAPTERS``)
and feed each :class:`PortfolioEntry` through :func:`auto_create_company`,
which performs the find-or-create with fuzzy matching.

Commit cadence: one commit per portfolio entry so a mid-run crash leaves
the DB in a clean state — matches the pattern in ``resolve_homepages.py``.

Adapter failure isolation: if one VC's adapter raises (their site is down,
HTML changed, etc.) we log the exception, record it in the summary, and
continue with the remaining adapters. One broken site never blocks the
other six.

Idempotency: every entry routes through ``auto_create_company``, which is
itself idempotent. Re-running the stage produces zero new rows for any
unchanged portfolio entry.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.upsert import (
    auto_create_company,
    link_company_investor,
    upsert_investor,
)
from nous.pipeline.refresh_investor_counts import refresh_investor_counts
from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios import ADAPTERS, FIRM_DISPLAY_NAMES, PortfolioEntry
from nous.util.url import is_storable_website

logger = logging.getLogger(__name__)

# Re-export PortfolioEntry so callers and tests can import it from this
# module without reaching into the sources tree.
__all__ = ["ADAPTERS", "PortfolioEntry", "RefreshVcPortfoliosSummary", "run_refresh_vc_portfolios"]


class RefreshVcPortfoliosSummary(BaseModel):
    """Outcome of one ``refresh-vc-portfolios`` run."""

    firms_run: int = 0
    entries_seen: int = 0
    companies_matched: int = 0
    """auto_create_company returned (row, False) — found existing match."""
    companies_created: int = 0
    """auto_create_company returned (row, True) — inserted new row."""
    investors_linked: int = 0
    """Per-entry company<->investor links written (one per successful entry)."""
    adapter_failures: dict[str, str] = {}
    """firm slug -> error message; missing = success."""


async def run_refresh_vc_portfolios(
    session: AsyncSession,
    client: HomepageClient,
    *,
    firms: list[str] | None = None,
    similarity_threshold: float = 0.85,
) -> RefreshVcPortfoliosSummary:
    """Walk every (or a selected subset of) VC adapter and auto-create rows.

    Args:
        session: An open async SQLAlchemy session. The stage commits per
            entry, not per firm, so a crash mid-firm leaves the DB
            consistent.
        client: An entered :class:`HomepageClient` — the adapters that need
            HTTP transport use this instance.
        firms: Optional list of firm slugs (matching keys in ``ADAPTERS``).
            ``None`` means run every adapter. Unknown slugs are recorded in
            ``summary.adapter_failures`` as ``"unknown firm"`` and do not
            increment ``firms_run``.
        similarity_threshold: pg_trgm threshold forwarded to
            :func:`auto_create_company`.

    Returns:
        A :class:`RefreshVcPortfoliosSummary` with per-stage counts and any
        adapter-level failures.
    """
    summary = RefreshVcPortfoliosSummary()

    selected = firms if firms is not None else list(ADAPTERS.keys())

    for firm_slug in selected:
        adapter = ADAPTERS.get(firm_slug)
        if adapter is None:
            logger.warning("vc adapter %s is not registered", firm_slug)
            summary.adapter_failures[firm_slug] = "unknown firm"
            continue

        summary.firms_run += 1

        try:
            entries = await adapter.fetch(client)
        except Exception as exc:  # noqa: BLE001 — adapter failure isolation is the point
            logger.exception("vc adapter %s failed", firm_slug)
            summary.adapter_failures[firm_slug] = repr(exc)
            continue

        for entry in entries:
            summary.entries_seen += 1
            # is_storable_website already rejects None/blank; the extra truthy
            # check on entry.website lets the type checker narrow str | None to
            # str so .strip() is well-typed (semantics are unchanged).
            storable_website = (
                entry.website.strip()
                if entry.website and is_storable_website(entry.website)
                else None
            )
            try:
                company, created = await auto_create_company(
                    session,
                    name=entry.name,
                    website=storable_website,
                    discovered_via="vc_portfolio",
                    similarity_threshold=similarity_threshold,
                )
                if created:
                    summary.companies_created += 1
                else:
                    summary.companies_matched += 1

                # The discovering firm IS an investor in this company. Record
                # the company-level link with the firm's proper display name.
                # Both helpers are idempotent, so this commits atomically with
                # the company and is safe to re-run.
                display_name = FIRM_DISPLAY_NAMES.get(entry.firm, entry.firm)
                investor, _ = await upsert_investor(session, name=display_name)
                # Known VC portfolio firms are always institutional investors.
                # Set the type on insert AND on re-run (idempotent update).
                if investor.type != "institutional":
                    investor.type = "institutional"
                    session.add(investor)
                await link_company_investor(
                    session,
                    company_id=company.id,
                    investor_id=investor.id,
                    source="vc_portfolio",
                )
                summary.investors_linked += 1

                await session.commit()
            except Exception:  # noqa: BLE001 — keep going on per-entry failure
                logger.exception(
                    "auto_create_company failed for entry %r from %s",
                    entry.name,
                    firm_slug,
                )
                # Don't increment matched/created on failure; rollback the
                # in-flight transaction so subsequent entries get a clean
                # session, then move on.
                await session.rollback()

    # Recompute portfolio_count for all investors now that VC portfolio links
    # may have changed. Committed separately from the per-entry commits above
    # so a count failure doesn't roll back the discovery work.
    await refresh_investor_counts(session)
    await session.commit()

    return summary
