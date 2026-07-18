"""clear-company-facts — standalone wrong-entity total/status clearer (ops).

delete-round's ``--clear-total`` / ``--clear-status`` flags ride on a ROUND
selection, which leaves a gap the 2026-07-18 re-heal hit twice:

- **wave**: its wrong-entity rounds were purged earlier and did NOT recur
  (articles aged out of the lookback), so no round exists to select — but
  the phantom "shut down" status (minted from a Primary-Wave-class article)
  persists.
- **terrafirma**: the wrong round was deleted in one apply, and only then
  was the stated $115M total discovered to be sourced OUTSIDE that purge
  set — with the round gone, the flag has nothing to ride on.

This lever clears the company-level facts directly: ``--clear-total`` wipes
``total_raised_usd/_source_url/_as_of``; ``--clear-status`` resets a
non-active status to active and clears ``status_source_url``. Each cleared
fact takes its ``fact_verifications`` rows with it (a ✓ minted against a
wrong-entity source must not survive the fact). At least one flag is
required; each no-ops (summary flag stays false) when there is nothing to
clear. Dry-run by default, previews the doomed values, idempotent.

The next enrichment/extraction pass can re-derive a CORRECT total/status
from right-entity sources — this lever removes poison, it does not blocklist
the fields.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification

logger = logging.getLogger(__name__)


class ClearCompanyFactsError(Exception):
    """Unknown company, or no flag given."""


class ClearCompanyFactsSummary(BaseModel):
    slug: str
    total_raised_cleared: bool = False
    total_raised_was: str | None = None
    total_raised_source_was: str | None = None
    status_reset: bool = False
    status_was: str | None = None
    status_source_was: str | None = None
    verifications_deleted: int = 0
    dry_run: bool = True


async def run_clear_company_facts(
    session: AsyncSession,
    *,
    slug: str,
    clear_total: bool = False,
    clear_status: bool = False,
    dry_run: bool = True,
) -> ClearCompanyFactsSummary:
    """Clear a company's stated total and/or non-active status. See module doc."""
    if not clear_total and not clear_status:
        raise ClearCompanyFactsError(
            "nothing to do — pass --clear-total and/or --clear-status"
        )
    company = (
        await session.execute(select(Company).where(Company.slug == slug))
    ).scalar_one_or_none()
    if company is None:
        raise ClearCompanyFactsError(f"no company with slug {slug!r}")

    summary = ClearCompanyFactsSummary(slug=slug, dry_run=dry_run)
    if clear_total and company.total_raised_usd is not None:
        summary.total_raised_cleared = True
        summary.total_raised_was = f"${company.total_raised_usd:,.0f}"
        summary.total_raised_source_was = company.total_raised_source_url
    if clear_status and company.status not in (None, "active"):
        summary.status_reset = True
        summary.status_was = company.status
        summary.status_source_was = company.status_source_url

    cleared_kinds: list[str] = []
    if summary.total_raised_cleared:
        cleared_kinds.append("total_raised")
    if summary.status_reset:
        cleared_kinds.append("status")
    if cleared_kinds:
        summary.verifications_deleted = len(
            (
                await session.execute(
                    select(FactVerification.id).where(
                        FactVerification.company_id == company.id,
                        FactVerification.fact_kind.in_(cleared_kinds),
                    )
                )
            )
            .scalars()
            .all()
        )

    logger.info(
        "clear-company-facts%s: %s — total_cleared=%s status_reset=%s "
        "verifications=%d",
        " (dry-run)" if dry_run else "",
        slug,
        summary.total_raised_cleared,
        summary.status_reset,
        summary.verifications_deleted,
    )
    if dry_run:
        return summary

    if cleared_kinds:
        await session.execute(
            delete(FactVerification).where(
                FactVerification.company_id == company.id,
                FactVerification.fact_kind.in_(cleared_kinds),
            )
        )
    if summary.total_raised_cleared:
        company.total_raised_usd = None
        company.total_raised_source_url = None
        company.total_raised_as_of = None
    if summary.status_reset:
        company.status = "active"
        company.status_source_url = None
    await session.commit()
    return summary
