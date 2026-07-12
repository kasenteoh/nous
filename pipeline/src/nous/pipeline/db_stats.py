"""DB size watchdog stage.

Reports per-table and total database sizes via SQLAlchemy expressions (no raw
SQL strings, per CLAUDE.md). Emits a GitHub Actions step summary table and
logs a loud warning when usage is approaching the Supabase 500 MB free-tier
cap.

The stage is read-only and idempotent — safe to run at any time.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, field_validator
from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import REGCLASS
from sqlalchemy.ext.asyncio import AsyncSession

import nous.db.models as _models  # noqa: F401  — registers tables with Base.metadata
from nous.db.base import Base
from nous.db.models import Company
from nous.observability import write_step_summary

logger = logging.getLogger(__name__)

# The curl_cffi Chrome-impersonation bypass (PR #132) landed 2026-07-10. Shown
# companies resolved BEFORE it that hit a Cloudflare 403 on every TLD candidate
# were stamped website_resolved_at with a null website by the weaker resolver;
# the 90-day re-resolve window won't retry them for months. These counts measure
# that stuck cohort (and how much a generation-cutoff re-drain would unstick NOW)
# so the fix is gated on a real number before it triggers any scrape/describe.
_RESOLVER_GENERATION_SINCE = datetime(2026, 7, 10, tzinfo=UTC)
_RESOLVE_REFETCH_DAYS = 90


class TableSize(BaseModel):
    """Size of a single table (total relation size includes indexes + toast)."""

    name: str
    bytes: int


class DbStatsSummary(BaseModel):
    """Result of one db-stats run."""

    tables: list[TableSize]  # sorted descending by bytes (enforced by validator)
    total_bytes: int
    cap_bytes: int
    pct_of_cap: float
    warn: bool  # True when pct_of_cap >= warn_pct
    # Website-less-husk cohort watch (default 0 so unit constructors stay valid).
    website_null_shown: int = 0  # shown (not excluded), no resolved website
    website_null_shown_funded: int = 0  # …of those, with ≥1 funding round
    drain_unblocks: int = 0  # …a generation-cutoff re-drain would unstick NOW

    @field_validator("tables")
    @classmethod
    def _sort_tables_descending(cls, v: list[TableSize]) -> list[TableSize]:
        """Always store tables largest-first, regardless of insertion order."""
        return sorted(v, key=lambda t: t.bytes, reverse=True)


async def run_db_stats(
    session: AsyncSession,
    *,
    cap_mb: int,
    warn_pct: int,
) -> DbStatsSummary:
    """Query per-table and total DB sizes; return a DbStatsSummary.

    Uses ``pg_total_relation_size`` (includes indexes + TOAST) via SQLAlchemy
    function expressions — no raw SQL strings (CLAUDE.md).
    """
    cap_bytes = cap_mb * 1024 * 1024

    table_names = list(Base.metadata.tables.keys())

    # Per-table sizes via pg_total_relation_size(REGCLASS).
    # We cast a literal string to REGCLASS so Postgres resolves the OID for us.
    # Injection is structurally impossible: names come from code-owned
    # Base.metadata (not user input), and cast() renders a bound parameter so
    # the driver sends the value separately from the SQL text.
    # to_regclass() returns NULL (vs. raise) if the schema ever lags migration;
    # the `or 0` below absorbs that gracefully.
    table_sizes: list[TableSize] = []
    for name in table_names:
        row = await session.execute(
            select(
                func.pg_total_relation_size(
                    cast(name, REGCLASS)
                )
            )
        )
        size_bytes: int = row.scalar_one() or 0
        table_sizes.append(TableSize(name=name, bytes=size_bytes))

    table_sizes.sort(key=lambda t: t.bytes, reverse=True)

    # Total database size.
    total_row = await session.execute(
        select(func.pg_database_size(func.current_database()))
    )
    total_bytes: int = total_row.scalar_one() or 0

    pct = (total_bytes / cap_bytes * 100) if cap_bytes > 0 else 0.0
    warn = pct >= warn_pct

    # Website-less-husk cohort counts (read-only; gates the re-drain fix).
    shown_no_website = Company.website.is_(None) & Company.exclusion_reason.is_(None)
    website_null_shown = (
        await session.execute(
            select(func.count()).select_from(Company).where(shown_no_website)
        )
    ).scalar_one()
    website_null_shown_funded = (
        await session.execute(
            select(func.count())
            .select_from(Company)
            .where(shown_no_website, Company.funding_round_count > 0)
        )
    ).scalar_one()
    # What a re-drain would unstick NOW: resolved by the pre-#132 weak resolver
    # (website_resolved_at < the bypass date) but still inside the 90-day window,
    # so the standing cadence won't re-admit them for months on its own.
    window_start = datetime.now(UTC) - timedelta(days=_RESOLVE_REFETCH_DAYS)
    drain_unblocks = (
        await session.execute(
            select(func.count())
            .select_from(Company)
            .where(
                shown_no_website,
                Company.website_resolved_at.is_not(None),
                Company.website_resolved_at >= window_start,
                Company.website_resolved_at < _RESOLVER_GENERATION_SINCE,
            )
        )
    ).scalar_one()

    return DbStatsSummary(
        tables=table_sizes,
        total_bytes=total_bytes,
        cap_bytes=cap_bytes,
        pct_of_cap=round(pct, 2),
        warn=warn,
        website_null_shown=website_null_shown,
        website_null_shown_funded=website_null_shown_funded,
        drain_unblocks=drain_unblocks,
    )


def emit_db_stats_summary(summary: DbStatsSummary) -> None:
    """Write a markdown table to the GitHub Actions step summary (if in CI)."""
    total_mb = summary.total_bytes / (1024 * 1024)
    cap_mb = summary.cap_bytes / (1024 * 1024)

    rows = "\n".join(
        f"| {t.name} | {t.bytes / 1024:.1f} KB |" for t in summary.tables
    )
    warn_badge = " :warning: **OVER WARN THRESHOLD**" if summary.warn else ""
    md = (
        f"\n### DB size report{warn_badge}\n\n"
        f"Total: **{total_mb:.1f} MB** of {cap_mb:.0f} MB cap "
        f"({summary.pct_of_cap:.1f}%)\n\n"
        f"| table | total size |\n"
        f"| --- | --- |\n"
        f"{rows}\n\n"
        f"**Website-less husks** (shown, no resolved website): "
        f"**{summary.website_null_shown}** "
        f"({summary.website_null_shown_funded} funded); "
        f"**{summary.drain_unblocks}** a re-drain would unstick now\n\n"
    )
    write_step_summary(md)
