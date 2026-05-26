from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from nous.db.base import Base


class Company(Base):
    """Represents a software company discovered via SEC Form D filings."""

    __tablename__ = "companies"

    # SEC Central Index Key — nullable because we may create companies before
    # linking to a CIK (e.g., from enriched sources in later milestones).
    cik: Mapped[str | None] = mapped_column(unique=True, nullable=True, index=True)
    name: Mapped[str]
    slug: Mapped[str] = mapped_column(unique=True, index=True)
    normalized_name: Mapped[str] = mapped_column(index=True)

    # LLM-enriched fields (populated in M2+)
    description_short: Mapped[str | None]
    description_long: Mapped[str | None]
    website: Mapped[str | None]
    logo_url: Mapped[str | None]

    # Location
    hq_city: Mapped[str | None]
    hq_state: Mapped[str | None]
    hq_country: Mapped[str | None] = mapped_column(default="US")

    # Company metadata
    year_incorporated: Mapped[int | None]
    industry_group: Mapped[str | None]

    # Employee count — stored as a range to accommodate estimated sources
    employee_count_min: Mapped[int | None]
    employee_count_max: Mapped[int | None]
    employee_count_source: Mapped[str | None]

    # Tracks when LLM enrichment last ran for this company
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # M2 enrichment fields
    primary_category: Mapped[str | None]
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    last_enriched_payload: Mapped[dict | None] = mapped_column(JSONB)  # type: ignore[type-arg]
    website_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Filing(Base):
    """Represents a single SEC Form D filing, linked to a Company."""

    __tablename__ = "filings"

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
    )
    accession_number: Mapped[str] = mapped_column(unique=True, index=True)
    filing_date: Mapped[date]

    # Financial fields — Numeric(20, 2) for precision with large dollar amounts
    offering_amount_total: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    amount_sold: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    investors_count: Mapped[int | None]
    minimum_investment: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))

    # Full raw XML/JSON payload from SEC EDGAR, stored for audit and re-extraction
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)  # type: ignore[type-arg]


class RelatedPerson(Base):
    """A person named in a Form D filing (e.g., executive director, promoter).

    Defined now per spec §5.1; populated during filing ingestion in M1 ingest stage.
    """

    __tablename__ = "related_persons"

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
    )
    filing_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("filings.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str]
    relationship: Mapped[str]
    # Address stored as a flexible dict; structure mirrors SEC EDGAR's address object
    address: Mapped[dict | None] = mapped_column(JSONB)  # type: ignore[type-arg]


class RawPage(Base):
    """Raw HTML cache for homepage / about / product pages.

    Per spec §4.8 + §5.3. Unique on (company_id, url) so the scraper
    can idempotently overwrite content+fetched_at with ON CONFLICT.
    """

    __tablename__ = "raw_pages"
    __table_args__ = (
        UniqueConstraint("company_id", "url", name="uq_raw_pages_company_url"),
    )

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
    )
    url: Mapped[str]
    content: Mapped[str]
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
