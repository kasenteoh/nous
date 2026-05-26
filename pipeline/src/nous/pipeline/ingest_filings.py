"""ingest-filings pipeline stage.

Searches SEC EDGAR for Form D filings in a date range, filters to the
configured industry groups, and upserts the results into the database.

Commit cadence: one ``session.commit()`` per successfully-inserted filing.
This means a crash mid-batch leaves a consistent state — already-processed
filings are durable and the next run will skip them via ON CONFLICT DO NOTHING.

NOTE: This function owns its own commit semantics (it commits per-filing
inside the loop).  Callers should pass a raw ``AsyncSession`` opened from
``AsyncSessionLocal()`` directly — do NOT use the ``get_session()`` context
manager here, as its auto-commit-on-exit would double-commit.
"""

from __future__ import annotations

import logging
from datetime import date

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.upsert import insert_filing_if_new, replace_related_persons, upsert_company
from nous.sources.edgar import EdgarClient
from nous.sources.form_d import FormDParseError, parse_form_d

logger = logging.getLogger(__name__)


class IngestSummary(BaseModel):
    """Counts returned by ``run_ingest_filings`` for observability / logging."""

    filings_seen: int = 0
    filings_kept: int = 0
    companies_inserted: int = 0
    companies_updated: int = 0
    filings_inserted: int = 0
    related_persons_inserted: int = 0
    parse_errors: int = 0


async def run_ingest_filings(
    session: AsyncSession,
    edgar: EdgarClient,
    industry_groups: set[str],
    since: date,
    until: date,
) -> IngestSummary:
    """Search EDGAR for Form D filings in [since, until] and upsert to DB.

    Args:
        session: An open async DB session.  This function commits per-filing;
            callers must NOT commit externally.
        edgar: An open EdgarClient context manager.
        industry_groups: Only filings whose ``industry_group_type`` is in this
            set are persisted.  Pass ``set()`` to skip all (useful for tests).
        since: Start date (inclusive).
        until: End date (inclusive).

    Returns:
        IngestSummary with running counts.
    """
    summary = IngestSummary()

    async for hit in edgar.search_form_d(since, until):
        summary.filings_seen += 1

        # Fetch and parse the primary XML document.
        try:
            xml = await edgar.fetch_primary_doc(hit.cik, hit.accession_number)
            form_d = parse_form_d(
                xml,
                accession_number=hit.accession_number,
                filing_date=hit.filing_date,
            )
        except FormDParseError as exc:
            logger.warning(
                "Parse error for accession %s (CIK %s): %s",
                hit.accession_number,
                hit.cik,
                exc,
            )
            summary.parse_errors += 1
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Unexpected error fetching accession %s (CIK %s): %s",
                hit.accession_number,
                hit.cik,
                exc,
            )
            summary.parse_errors += 1
            continue

        # Industry filter.
        if form_d.industry_group_type not in industry_groups:
            continue

        summary.filings_kept += 1

        # Upsert company.
        company, created = await upsert_company(session, form_d)
        if created:
            summary.companies_inserted += 1
        else:
            summary.companies_updated += 1

        # Insert filing (idempotent).
        filing = await insert_filing_if_new(session, company.id, form_d)
        if filing is None:
            # Duplicate — already in DB; still commit any company updates.
            await session.commit()
            continue

        summary.filings_inserted += 1

        # Replace related persons for this filing.
        n = await replace_related_persons(
            session, company.id, filing.id, form_d.related_persons
        )
        summary.related_persons_inserted += n

        # Commit once per filing so crashes leave a clean state.
        await session.commit()

    return summary
