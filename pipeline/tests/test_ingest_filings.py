"""Integration tests for the ingest-filings pipeline stage.

Requires DATABASE_URL env var pointing at a live Postgres instance with the
schema already applied via ``alembic upgrade head``.

Tests are skipped when DATABASE_URL is unset or empty.

A mock EdgarClient is used so no real HTTP calls are made.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Filing
from nous.pipeline.ingest_filings import IngestSummary, run_ingest_filings
from nous.sources.edgar import EdgarClient, FilingHit

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"

# The sample XML uses "Other Technology" as its industry group; it is NOT one
# of the default INDUSTRY_GROUPS.  We use a synthetic XML for the main tests
# so we can control the industry group value, and use the real fixture only
# for the parse path.
_SAMPLE_XML = (FIXTURES_DIR / "form_d_sample.xml").read_text()

# A minimal valid Form D XML with industryGroupType set to a tech group.
_TECH_XML_TEMPLATE = """<?xml version="1.0"?>
<edgarSubmission>
  <schemaVersion>X0708</schemaVersion>
  <submissionType>D</submissionType>
  <testOrLive>LIVE</testOrLive>
  <primaryIssuer>
    <cik>{cik}</cik>
    <entityName>{entity_name}</entityName>
    <issuerAddress>
      <street1>123 Main St</street1>
      <city>San Francisco</city>
      <stateOrCountry>CA</stateOrCountry>
      <zipCode>94107</zipCode>
    </issuerAddress>
    <entityType>Corporation</entityType>
    <yearOfInc>
      <yearOfIncValue>2020</yearOfIncValue>
    </yearOfInc>
  </primaryIssuer>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName>
        <firstName>Jane</firstName>
        <lastName>Doe</lastName>
      </relatedPersonName>
      <relatedPersonAddress>
        <city>San Francisco</city>
        <stateOrCountry>CA</stateOrCountry>
      </relatedPersonAddress>
      <relatedPersonRelationshipList>
        <relationship>Executive Officer</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
  <offeringData>
    <industryGroup>
      <industryGroupType>{industry_group}</industryGroupType>
    </industryGroup>
    <minimumInvestmentAccepted>0</minimumInvestmentAccepted>
    <salesCompensationList/>
    <offeringSalesAmounts>
      <totalOfferingAmount>1000000</totalOfferingAmount>
      <totalAmountSold>500000</totalAmountSold>
      <totalRemaining>500000</totalRemaining>
    </offeringSalesAmounts>
    <investors>
      <hasNonAccreditedInvestors>false</hasNonAccreditedInvestors>
      <totalNumberAlreadyInvested>3</totalNumberAlreadyInvested>
    </investors>
  </offeringData>
</edgarSubmission>"""


def _make_xml(
    cik: str = "0009991111",
    entity_name: str = "MockCo Inc.",
    industry_group: str = "Technology - Computers",
) -> str:
    return _TECH_XML_TEMPLATE.format(
        cik=cik, entity_name=entity_name, industry_group=industry_group
    )


class MockEdgarClient(EdgarClient):
    """EdgarClient subclass that returns canned data without HTTP calls."""

    def __init__(
        self,
        hits: list[FilingHit],
        xml_by_accession: dict[str, str],
    ) -> None:
        # Pass a dummy user_agent to satisfy validation.
        super().__init__(user_agent="test agent test@example.com")
        self._hits = hits
        self._xml_by_accession = xml_by_accession

    async def __aenter__(self) -> MockEdgarClient:  # type: ignore[override]
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def search_form_d(  # type: ignore[override]
        self, start: date, end: date
    ) -> AsyncIterator[FilingHit]:
        for hit in self._hits:
            yield hit

    async def fetch_primary_doc(self, cik: str, accession_number: str) -> str:
        return self._xml_by_accession[accession_number]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TECH_GROUPS: set[str] = {"Technology - Computers", "Technology - Other"}


def _make_hit(
    cik: str = "0009991111",
    accession: str = "0009991111-21-000001",
    entity_name: str = "MockCo Inc.",
    filing_date: date = date(2021, 6, 1),
) -> FilingHit:
    return FilingHit(
        accession_number=accession,
        cik=cik,
        entity_name=entity_name,
        filing_date=filing_date,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_first_run_inserts_data(db: AsyncSession) -> None:
    """First run populates companies, filings, and related persons."""
    hit = _make_hit()
    xml = _make_xml()
    edgar = MockEdgarClient(hits=[hit], xml_by_accession={hit.accession_number: xml})

    summary: IngestSummary = await run_ingest_filings(
        db, edgar, TECH_GROUPS, since=date(2021, 1, 1), until=date(2021, 12, 31)
    )

    assert summary.filings_seen == 1
    assert summary.filings_kept == 1
    assert summary.filings_inserted == 1
    assert summary.companies_inserted == 1
    assert summary.companies_updated == 0
    assert summary.parse_errors == 0
    assert summary.related_persons_inserted >= 1

    # Verify rows actually exist in DB.
    companies = (await db.execute(select(Company))).scalars().all()
    assert any(c.cik == "0009991111" for c in companies)


async def test_second_run_is_idempotent(db: AsyncSession) -> None:
    """Running with the same inputs twice inserts nothing on the second pass."""
    hit = _make_hit(cik="0009992222", accession="0009992222-21-000001")
    xml = _make_xml(cik="0009992222", entity_name="IdemCo Inc.")
    edgar = MockEdgarClient(hits=[hit], xml_by_accession={hit.accession_number: xml})

    summary1: IngestSummary = await run_ingest_filings(
        db, edgar, TECH_GROUPS, since=date(2021, 1, 1), until=date(2021, 12, 31)
    )
    assert summary1.filings_inserted == 1
    assert summary1.companies_inserted == 1

    # Second run — same edgar mock.
    edgar2 = MockEdgarClient(hits=[hit], xml_by_accession={hit.accession_number: xml})
    summary2: IngestSummary = await run_ingest_filings(
        db, edgar2, TECH_GROUPS, since=date(2021, 1, 1), until=date(2021, 12, 31)
    )
    assert summary2.filings_inserted == 0
    assert summary2.companies_inserted == 0


async def test_industry_filter_excludes_non_tech(db: AsyncSession) -> None:
    """Filings outside the configured industry groups are not persisted."""
    hit = _make_hit(cik="0009993333", accession="0009993333-21-000001")
    # XML with a non-tech industry group.
    xml = _make_xml(
        cik="0009993333",
        entity_name="OilCo Inc.",
        industry_group="Oil and Gas",
    )
    edgar = MockEdgarClient(hits=[hit], xml_by_accession={hit.accession_number: xml})

    summary: IngestSummary = await run_ingest_filings(
        db, edgar, TECH_GROUPS, since=date(2021, 1, 1), until=date(2021, 12, 31)
    )

    assert summary.filings_seen == 1
    assert summary.filings_kept == 0
    assert summary.filings_inserted == 0
    assert summary.companies_inserted == 0


async def test_empty_industry_groups_skips_all(db: AsyncSession) -> None:
    """Passing an empty set skips every filing."""
    hit = _make_hit(cik="0009994444", accession="0009994444-21-000001")
    xml = _make_xml(cik="0009994444", entity_name="AnyTech Inc.")
    edgar = MockEdgarClient(hits=[hit], xml_by_accession={hit.accession_number: xml})

    summary: IngestSummary = await run_ingest_filings(
        db, edgar, set(), since=date(2021, 1, 1), until=date(2021, 12, 31)
    )

    assert summary.filings_kept == 0
    assert summary.filings_inserted == 0


async def test_parse_error_counted_and_skipped(db: AsyncSession) -> None:
    """A malformed XML increments parse_errors and does not abort the run."""
    hit_bad = _make_hit(cik="0009995555", accession="0009995555-21-000001")
    hit_good = _make_hit(cik="0009995556", accession="0009995556-21-000001")
    xml_good = _make_xml(cik="0009995556", entity_name="GoodCo Inc.")

    edgar = MockEdgarClient(
        hits=[hit_bad, hit_good],
        xml_by_accession={
            hit_bad.accession_number: "THIS IS NOT XML <<<",
            hit_good.accession_number: xml_good,
        },
    )

    summary: IngestSummary = await run_ingest_filings(
        db, edgar, TECH_GROUPS, since=date(2021, 1, 1), until=date(2021, 12, 31)
    )

    assert summary.parse_errors == 1
    assert summary.filings_inserted == 1
    assert summary.companies_inserted == 1


async def test_multiple_filings_same_company(db: AsyncSession) -> None:
    """Two filings for the same CIK result in one company and two filings."""
    cik = "0009996666"
    hit1 = _make_hit(cik=cik, accession=f"{cik}-21-000001", filing_date=date(2021, 1, 1))
    hit2 = _make_hit(cik=cik, accession=f"{cik}-22-000001", filing_date=date(2022, 1, 1))
    xml1 = _make_xml(cik=cik, entity_name="MultiFilingCo Inc.")
    xml2 = _make_xml(cik=cik, entity_name="MultiFilingCo Inc.")

    edgar = MockEdgarClient(
        hits=[hit1, hit2],
        xml_by_accession={
            hit1.accession_number: xml1,
            hit2.accession_number: xml2,
        },
    )

    summary: IngestSummary = await run_ingest_filings(
        db, edgar, TECH_GROUPS, since=date(2021, 1, 1), until=date(2022, 12, 31)
    )

    assert summary.filings_inserted == 2
    # First was an insert, second was an update.
    assert summary.companies_inserted == 1
    assert summary.companies_updated == 1

    # Confirm exactly one company row and two filing rows.
    companies = list(
        (await db.execute(select(Company).where(Company.cik == cik))).scalars().all()
    )
    assert len(companies) == 1

    filings = list(
        (
            await db.execute(select(Filing).where(Filing.company_id == companies[0].id))
        ).scalars().all()
    )
    assert len(filings) == 2
