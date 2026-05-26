"""Tests for the Form D XML parser (nous.sources.form_d)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nous.sources.form_d import (
    FormD,
    FormDParseError,
    parse_form_d,
)

FIXTURES = Path(__file__).parent / "fixtures"

ACCESSION = "0001858523-26-000003"
FILING_DATE = date(2026, 5, 1)


# ---------------------------------------------------------------------------
# Real fixture — Persefoni AI Inc.
# ---------------------------------------------------------------------------


def _parsed_persefoni() -> FormD:
    xml = (FIXTURES / "form_d_sample.xml").read_text()
    return parse_form_d(xml, accession_number=ACCESSION, filing_date=FILING_DATE)


def test_real_fixture_entity_name() -> None:
    result = _parsed_persefoni()
    assert result.entity_name == "Persefoni AI Inc."


def test_real_fixture_cik() -> None:
    result = _parsed_persefoni()
    assert result.cik == "0001858523"


def test_real_fixture_accession_passthrough() -> None:
    result = _parsed_persefoni()
    assert result.accession_number == ACCESSION


def test_real_fixture_filing_date_passthrough() -> None:
    result = _parsed_persefoni()
    assert result.filing_date == FILING_DATE


def test_real_fixture_entity_type() -> None:
    result = _parsed_persefoni()
    assert result.entity_type == "Corporation"


def test_real_fixture_industry_group_type() -> None:
    result = _parsed_persefoni()
    assert result.industry_group_type == "Other Technology"


def test_real_fixture_year_of_incorporation_none() -> None:
    # Persefoni AI uses <overFiveYears>true</overFiveYears> — should parse to None.
    result = _parsed_persefoni()
    assert result.year_of_incorporation is None


def test_real_fixture_principal_place_of_business() -> None:
    result = _parsed_persefoni()
    addr = result.principal_place_of_business
    assert addr.street == "2415 W. BROADWAY ROAD #41022"
    assert addr.city == "MESA"
    assert addr.state == "AZ"
    assert addr.zip == "85202"


def test_real_fixture_offering_amounts() -> None:
    result = _parsed_persefoni()
    assert result.total_offering_amount == Decimal("4349962")
    assert result.total_amount_sold == Decimal("4349962")
    assert result.total_remaining == Decimal("0")


def test_real_fixture_minimum_investment() -> None:
    result = _parsed_persefoni()
    assert result.minimum_investment_accepted == Decimal("0")


def test_real_fixture_investors_count() -> None:
    result = _parsed_persefoni()
    assert result.total_number_already_invested == 6


def test_real_fixture_related_persons_count() -> None:
    # The fixture has 9 people. Jason Offerman and Kentaro Kawamori each have
    # 2 roles (Executive Officer + Director), so we expect 9 + 2 extra = 11 records.
    result = _parsed_persefoni()
    assert len(result.related_persons) == 11


def test_real_fixture_first_person_name() -> None:
    result = _parsed_persefoni()
    names = [p.name for p in result.related_persons]
    assert "Jason Frederick Offerman" in names


def test_real_fixture_multi_role_person_roles() -> None:
    """Jason Offerman has both 'Executive Officer' and 'Director' roles."""
    result = _parsed_persefoni()
    jason_roles = {
        p.relationship for p in result.related_persons if p.name == "Jason Frederick Offerman"
    }
    assert "Executive Officer" in jason_roles
    assert "Director" in jason_roles


def test_real_fixture_single_role_person() -> None:
    """Daniel Rice IV has only the Director role."""
    result = _parsed_persefoni()
    daniel_records = [p for p in result.related_persons if p.name == "Daniel Rice IV"]
    assert len(daniel_records) == 1
    assert daniel_records[0].relationship == "Director"


def test_real_fixture_person_address() -> None:
    result = _parsed_persefoni()
    jason = next(p for p in result.related_persons if p.name == "Jason Frederick Offerman")
    assert jason.address is not None
    assert jason.address.city == "Mesa"
    assert jason.address.state == "AZ"


# ---------------------------------------------------------------------------
# year_of_incorporation enum cases — inline XML
# ---------------------------------------------------------------------------

_YEAR_TEMPLATE = """\
<?xml version="1.0"?>
<edgarSubmission>
  <primaryIssuer>
    <cik>0009999999</cik>
    <entityName>Test Corp</entityName>
    <issuerAddress>
      <street1>1 Main St</street1>
      <city>Anytown</city>
      <stateOrCountry>CA</stateOrCountry>
      <zipCode>90001</zipCode>
    </issuerAddress>
    <entityType>Corporation</entityType>
    <yearOfInc>
      {year_block}
    </yearOfInc>
  </primaryIssuer>
  <relatedPersonsList/>
  <offeringData>
    <industryGroup>
      <industryGroupType>Technology</industryGroupType>
    </industryGroup>
    <minimumInvestmentAccepted>0</minimumInvestmentAccepted>
    <offeringSalesAmounts>
      <totalOfferingAmount>1000000</totalOfferingAmount>
      <totalAmountSold>500000</totalAmountSold>
      <totalRemaining>500000</totalRemaining>
    </offeringSalesAmounts>
    <investors>
      <totalNumberAlreadyInvested>3</totalNumberAlreadyInvested>
    </investors>
  </offeringData>
</edgarSubmission>
"""


def _parse_inline(year_block: str) -> FormD:
    xml = _YEAR_TEMPLATE.format(year_block=year_block)
    return parse_form_d(xml, accession_number="0009999999-25-000001", filing_date=date(2025, 1, 1))


def test_year_over_five_years_ago_is_none() -> None:
    result = _parse_inline("<overFiveYears>true</overFiveYears>")
    assert result.year_of_incorporation is None


def test_year_within_five_years_is_none() -> None:
    result = _parse_inline("<withinFiveYears>true</withinFiveYears>")
    assert result.year_of_incorporation is None


def test_year_explicit_value_parsed() -> None:
    result = _parse_inline("<yearOfIncValue>2021</yearOfIncValue>")
    assert result.year_of_incorporation == 2021


# ---------------------------------------------------------------------------
# Multi-role inline XML
# ---------------------------------------------------------------------------

_MULTI_ROLE_XML = """\
<?xml version="1.0"?>
<edgarSubmission>
  <primaryIssuer>
    <cik>0009999998</cik>
    <entityName>Multi Role Corp</entityName>
    <issuerAddress>
      <street1>2 Main St</street1>
      <city>Techville</city>
      <stateOrCountry>CA</stateOrCountry>
      <zipCode>90002</zipCode>
    </issuerAddress>
    <entityType>Corporation</entityType>
    <yearOfInc>
      <yearOfIncValue>2020</yearOfIncValue>
    </yearOfInc>
  </primaryIssuer>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName>
        <firstName>Alice</firstName>
        <lastName>Smith</lastName>
      </relatedPersonName>
      <relatedPersonAddress>
        <street1>2 Main St</street1>
        <city>Techville</city>
        <stateOrCountry>CA</stateOrCountry>
        <zipCode>90002</zipCode>
      </relatedPersonAddress>
      <relatedPersonRelationshipList>
        <relationship>Director</relationship>
        <relationship>Officer</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
    <relatedPersonInfo>
      <relatedPersonName>
        <firstName>Bob</firstName>
        <lastName>Jones</lastName>
      </relatedPersonName>
      <relatedPersonRelationshipList>
        <relationship>Director</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
  <offeringData>
    <industryGroup>
      <industryGroupType>Technology - Computers</industryGroupType>
    </industryGroup>
    <minimumInvestmentAccepted>50000</minimumInvestmentAccepted>
    <offeringSalesAmounts>
      <totalOfferingAmount>2000000</totalOfferingAmount>
      <totalAmountSold>1000000</totalAmountSold>
      <totalRemaining>1000000</totalRemaining>
    </offeringSalesAmounts>
    <investors>
      <totalNumberAlreadyInvested>2</totalNumberAlreadyInvested>
    </investors>
  </offeringData>
</edgarSubmission>
"""


def test_multi_role_person_emits_two_records() -> None:
    result = parse_form_d(
        _MULTI_ROLE_XML,
        accession_number="0009999998-25-000001",
        filing_date=date(2025, 1, 1),
    )
    alice_records = [p for p in result.related_persons if p.name == "Alice Smith"]
    roles = {p.relationship for p in alice_records}
    assert roles == {"Director", "Officer"}
    assert len(alice_records) == 2


def test_multi_role_single_role_person_emits_one_record() -> None:
    result = parse_form_d(
        _MULTI_ROLE_XML,
        accession_number="0009999998-25-000001",
        filing_date=date(2025, 1, 1),
    )
    bob_records = [p for p in result.related_persons if p.name == "Bob Jones"]
    assert len(bob_records) == 1
    assert bob_records[0].relationship == "Director"


def test_multi_role_total_persons_count() -> None:
    result = parse_form_d(
        _MULTI_ROLE_XML,
        accession_number="0009999998-25-000001",
        filing_date=date(2025, 1, 1),
    )
    # Alice has 2 roles + Bob has 1 role = 3 records
    assert len(result.related_persons) == 3


# ---------------------------------------------------------------------------
# Malformed XML raises FormDParseError
# ---------------------------------------------------------------------------


def test_malformed_xml_raises_form_d_parse_error() -> None:
    with pytest.raises(FormDParseError):
        parse_form_d(
            "this is not xml at all <<<>",
            accession_number="0009999997-25-000001",
            filing_date=date(2025, 1, 1),
        )


def test_empty_string_raises_form_d_parse_error() -> None:
    with pytest.raises(FormDParseError):
        parse_form_d(
            "",
            accession_number="0009999997-25-000001",
            filing_date=date(2025, 1, 1),
        )


# ---------------------------------------------------------------------------
# Graceful handling of missing optional fields
# ---------------------------------------------------------------------------

_MINIMAL_XML = """\
<?xml version="1.0"?>
<edgarSubmission>
  <primaryIssuer>
    <cik>0000000001</cik>
    <entityName>Minimal Corp</entityName>
    <issuerAddress/>
  </primaryIssuer>
  <relatedPersonsList/>
  <offeringData>
    <industryGroup>
      <industryGroupType>Technology</industryGroupType>
    </industryGroup>
  </offeringData>
</edgarSubmission>
"""


def test_minimal_xml_no_error() -> None:
    result = parse_form_d(
        _MINIMAL_XML,
        accession_number="0000000001-25-000001",
        filing_date=date(2025, 1, 1),
    )
    assert result.entity_name == "Minimal Corp"
    assert result.total_offering_amount is None
    assert result.total_amount_sold is None
    assert result.total_remaining is None
    assert result.minimum_investment_accepted is None
    assert result.total_number_already_invested is None
    assert result.related_persons == []
    assert result.year_of_incorporation is None
    assert result.entity_type is None


def test_street2_concatenated() -> None:
    xml = """\
<?xml version="1.0"?>
<edgarSubmission>
  <primaryIssuer>
    <cik>0000000002</cik>
    <entityName>Street Corp</entityName>
    <issuerAddress>
      <street1>123 Main St</street1>
      <street2>Suite 400</street2>
      <city>San Francisco</city>
      <stateOrCountry>CA</stateOrCountry>
      <zipCode>94102</zipCode>
    </issuerAddress>
  </primaryIssuer>
  <relatedPersonsList/>
  <offeringData>
    <industryGroup><industryGroupType>Technology</industryGroupType></industryGroup>
  </offeringData>
</edgarSubmission>
"""
    result = parse_form_d(
        xml, accession_number="0000000002-25-000001", filing_date=date(2025, 1, 1)
    )
    assert result.principal_place_of_business.street == "123 Main St\nSuite 400"
