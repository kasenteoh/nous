from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
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

    # M3 — how this company first entered the DB.
    # 'form_d' | 'vc_portfolio' | 'news' | 'techcrunch'.
    discovered_via: Mapped[str] = mapped_column(
        String, nullable=False, server_default="form_d"
    )


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


class NewsArticle(Base):
    """Per spec §4.7. A news article mentioning a tracked company, captured for
    funding-extraction. Unique on canonical URL so re-ingest is a no-op.
    """

    __tablename__ = "news_articles"

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
    )
    url: Mapped[str] = mapped_column(unique=True, index=True)
    title: Mapped[str]
    source: Mapped[str]  # hostname, e.g. "techcrunch.com"
    published_date: Mapped[date | None] = mapped_column(Date)
    raw_content: Mapped[str]
    # processed=true marks the article as having been passed through
    # extract-funding; the work-queue index in 0003 is partial on this column.
    processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", index=True
    )


class FundingRound(Base):
    """Per spec §4.3. A funding round attributed to a company, optionally
    linked to a Form D filing and/or to a news article (primary_news_url).
    """

    __tablename__ = "funding_rounds"

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
    )
    round_type: Mapped[str | None]  # "Series A", "Seed", etc. — free text, light normalization
    amount_raised: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    valuation_post_money: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    valuation_source: Mapped[str | None]  # e.g. "TechCrunch, Mar 2026"
    announced_date: Mapped[date | None] = mapped_column(Date, index=True)
    filing_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("filings.id"),
        nullable=True,
        index=True,
    )
    primary_news_url: Mapped[str | None]
    # LLM-reported confidence: 'low' | 'medium' | 'high'.
    extraction_confidence: Mapped[str | None]


class Investor(Base):
    """Per spec §4.4 + M3 plan additions. `name_normalized` materializes the
    'unique on lowercased form' rule so we can do indexed lookups without
    LOWER() everywhere; `name` retains original display casing.
    """

    __tablename__ = "investors"

    name: Mapped[str]
    name_normalized: Mapped[str] = mapped_column(unique=True, index=True)
    # 'institutional' | 'angel' | 'unknown'
    type: Mapped[str] = mapped_column(String, nullable=False, server_default="unknown")
    description: Mapped[str | None]
    website: Mapped[str | None]


class FundingRoundInvestor(Base):
    """Per spec §4.5. Join table between funding_rounds and investors. Unique
    on the pair so a re-extracted article can't double-link the same investor
    to the same round; `is_lead` distinguishes lead vs participating.
    """

    __tablename__ = "funding_round_investors"
    __table_args__ = (
        UniqueConstraint(
            "funding_round_id",
            "investor_id",
            name="uq_funding_round_investors_round_investor",
        ),
    )

    funding_round_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("funding_rounds.id", ondelete="CASCADE"),
        index=True,
    )
    investor_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("investors.id", ondelete="CASCADE"),
        index=True,
    )
    is_lead: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
