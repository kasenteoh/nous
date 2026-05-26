"""Form D XML parser.

Parses the ``primary_doc.xml`` returned by SEC EDGAR for Form D filings into
typed Pydantic models. Fields not present in the XML are left as ``None`` (or
empty list for ``related_persons``) — we never fabricate values.

The XML has no namespace in the ``edgarSubmission`` schema (X0708), so plain
tag names work.  We use ``lxml.etree`` for robustness against minor XML quirks.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from lxml import etree
from pydantic import BaseModel

if TYPE_CHECKING:
    # lxml stubs are not shipped with lxml; we use Any at runtime but keep
    # descriptive aliases in annotations so the intent is clear.
    _Element = Any
else:
    _Element = Any


class FormDParseError(Exception):
    """Raised when the XML is malformed beyond recovery."""


class FormDAddress(BaseModel):
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None


class FormDRelatedPerson(BaseModel):
    name: str
    relationship: str  # e.g. "Director", "Executive Officer", "Promoter"
    address: FormDAddress | None = None


class FormD(BaseModel):
    accession_number: str
    cik: str
    entity_name: str
    industry_group_type: str
    year_of_incorporation: int | None = None
    entity_type: str | None = None
    principal_place_of_business: FormDAddress
    total_offering_amount: Decimal | None = None
    total_amount_sold: Decimal | None = None
    total_remaining: Decimal | None = None
    minimum_investment_accepted: Decimal | None = None
    total_number_already_invested: int | None = None
    related_persons: list[FormDRelatedPerson] = []
    filing_date: date


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _text(el: _Element | None, tag: str) -> str | None:
    """Return stripped text of the *first* child ``tag`` element, or None."""
    if el is None:
        return None
    child = el.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def _decimal(el: _Element | None, tag: str) -> Decimal | None:
    """Parse a decimal from a child element, returning None on failure."""
    raw = _text(el, tag)
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _int(el: _Element | None, tag: str) -> int | None:
    """Parse an int from a child element, returning None on failure."""
    raw = _text(el, tag)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_address(addr_el: _Element | None) -> FormDAddress:
    """Parse a street/city/state/zip block into FormDAddress."""
    if addr_el is None:
        return FormDAddress()

    street1 = _text(addr_el, "street1") or ""
    street2 = _text(addr_el, "street2") or ""
    street_parts = [p for p in [street1, street2] if p]
    street = "\n".join(street_parts) or None

    return FormDAddress(
        street=street,
        city=_text(addr_el, "city"),
        state=_text(addr_el, "stateOrCountry"),
        zip=_text(addr_el, "zipCode"),
        # Default to US when the country element is absent (domestic filings).
        country=_text(addr_el, "country") or "US",
    )


def _parse_year_of_inc(issuer_el: _Element | None) -> int | None:
    """Extract year_of_incorporation.

    Form D encodes this in two different ways:
    - ``<yearOfInc><yearOfIncValue>2021</yearOfIncValue></yearOfInc>`` → int
    - ``<yearOfInc><overFiveYears>true</overFiveYears></yearOfInc>`` → None
    - ``<yearOfInc><withinFiveYears>true</withinFiveYears></yearOfInc>`` → None

    If no integer can be coerced we return None; the raw XML is persisted
    upstream in ``filings.raw_data`` so the enum value is not lost.
    """
    if issuer_el is None:
        return None
    year_el = issuer_el.find("yearOfInc")
    if year_el is None:
        return None

    # Try the explicit integer value element first.
    for value_tag in ("yearOfIncValue", "value"):
        raw = _text(year_el, value_tag)
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                return None

    # Presence of a timespan flag (overFiveYears / withinFiveYears /
    # yetToBeFormed) → cannot coerce to int.
    return None


def _parse_related_persons(rpl_el: _Element | None) -> list[FormDRelatedPerson]:
    """Parse all relatedPersonInfo blocks into FormDRelatedPerson objects.

    A single person may list multiple roles in their
    ``<relatedPersonRelationshipList>``.  We emit one ``FormDRelatedPerson``
    per role so callers can query "all directors" without set intersection.
    """
    if rpl_el is None:
        return []

    persons: list[FormDRelatedPerson] = []
    for person_el in rpl_el.findall("relatedPersonInfo"):
        name_el = person_el.find("relatedPersonName")
        if name_el is None:
            continue

        # Concatenate first / middle / last with spaces, strip empty parts.
        name_parts = [
            _text(name_el, "firstName"),
            _text(name_el, "middleName"),
            _text(name_el, "lastName"),
        ]
        full_name = " ".join(p for p in name_parts if p)
        if not full_name:
            continue

        address = _parse_address(person_el.find("relatedPersonAddress"))

        rel_list_el = person_el.find("relatedPersonRelationshipList")
        relationships: list[str] = []
        if rel_list_el is not None:
            for rel_el in rel_list_el.findall("relationship"):
                if rel_el.text and rel_el.text.strip():
                    relationships.append(rel_el.text.strip())

        if not relationships:
            # Person with no declared role — still record them with blank role.
            persons.append(
                FormDRelatedPerson(name=full_name, relationship="", address=address)
            )
        else:
            for role in relationships:
                persons.append(
                    FormDRelatedPerson(name=full_name, relationship=role, address=address)
                )

    return persons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_form_d(
    xml_text: str,
    *,
    accession_number: str,
    filing_date: date,
) -> FormD:
    """Parse a Form D ``primary_doc.xml`` and return a :class:`FormD` model.

    Args:
        xml_text: The raw XML string fetched from EDGAR.
        accession_number: Passed in from the search hit; used directly because
            the XML body may contain a different or absent accession field.
        filing_date: Passed in from the search hit for the same reason.

    Raises:
        FormDParseError: If the XML is unparseable.
    """
    try:
        root = etree.fromstring(bytes(xml_text, "utf-8"))
    except etree.XMLSyntaxError as exc:
        raise FormDParseError(f"XML parse error: {exc}") from exc

    # ------------------------------------------------------------------
    # Primary issuer block
    # ------------------------------------------------------------------
    issuer_el = root.find("primaryIssuer")

    cik = _text(issuer_el, "cik") or ""
    entity_name = _text(issuer_el, "entityName") or ""
    entity_type = _text(issuer_el, "entityType")
    year_of_incorporation = _parse_year_of_inc(issuer_el)

    address_el = issuer_el.find("issuerAddress") if issuer_el is not None else None
    principal_place_of_business = _parse_address(address_el)

    # ------------------------------------------------------------------
    # Offering data
    # ------------------------------------------------------------------
    offering_el = root.find("offeringData")

    industry_group_type = ""
    if offering_el is not None:
        ig_el = offering_el.find("industryGroup")
        industry_group_type = _text(ig_el, "industryGroupType") or ""

    sales_el = (
        offering_el.find("offeringSalesAmounts") if offering_el is not None else None
    )
    total_offering_amount = _decimal(sales_el, "totalOfferingAmount")
    total_amount_sold = _decimal(sales_el, "totalAmountSold")
    total_remaining = _decimal(sales_el, "totalRemaining")

    minimum_investment_accepted = _decimal(offering_el, "minimumInvestmentAccepted")

    investors_el = (
        offering_el.find("investors") if offering_el is not None else None
    )
    total_number_already_invested = _int(investors_el, "totalNumberAlreadyInvested")

    # ------------------------------------------------------------------
    # Related persons
    # ------------------------------------------------------------------
    related_persons = _parse_related_persons(root.find("relatedPersonsList"))

    return FormD(
        accession_number=accession_number,
        cik=cik,
        entity_name=entity_name,
        industry_group_type=industry_group_type,
        year_of_incorporation=year_of_incorporation,
        entity_type=entity_type,
        principal_place_of_business=principal_place_of_business,
        total_offering_amount=total_offering_amount,
        total_amount_sold=total_amount_sold,
        total_remaining=total_remaining,
        minimum_investment_accepted=minimum_investment_accepted,
        total_number_already_invested=total_number_already_invested,
        related_persons=related_persons,
        filing_date=filing_date,
    )
