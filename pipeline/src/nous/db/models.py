from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from nous.db.base import Base


class Company(Base):
    """Represents a software company discovered from public sources.

    Companies enter the DB via VC portfolio scrapes, funding news, or the
    TechCrunch venture feed (see ``discovered_via``).

    ``status`` tracks the company's lifecycle ('active' | 'acquired' |
    'shut_down' | 'ipo') — VC portfolios list their exits, so without it
    acquired/dead companies would render as live startups. extract-funding
    sets it from explicit announcements; ``status_source_url`` records the
    article (or company page) that announced the event.
    """

    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'acquired', 'shut_down', 'ipo')",
            name="ck_companies_status",
        ),
    )

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
    # When the estimate-employees stage last attempted this company (success or
    # not) — drives the refetch-staleness / back-off eligibility query. Indexed
    # because it appears in that WHERE clause.
    employee_count_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # Tracks when LLM enrichment last ran for this company
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # M2 enrichment fields
    primary_category: Mapped[str | None]
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    last_enriched_payload: Mapped[dict | None] = mapped_column(JSONB)  # type: ignore[type-arg]
    website_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scrape_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # Consecutive homepage-fetch failures across scrape-homepages runs. Bumped
    # by one on a total fetch failure (network/HTTP error → no usable content),
    # reset to 0 on a successful homepage fetch, and left unchanged on a
    # robots.txt block (the site is alive, just disallowing us). When this
    # crosses a small threshold the web surfaces a muted "possibly inactive"
    # rider — a low-confidence signal, deliberately quieter than the status
    # badge. Not indexed: never used in a WHERE in the pipeline.
    consecutive_scrape_failures: Mapped[int] = mapped_column(
        nullable=False, server_default="0"
    )

    # When ingest-news last ran this company's Google News RSS query. Drives
    # the daily rotation: ORDER BY news_checked_at NULLS FIRST + LIMIT lets a
    # bounded run cover the whole table every ~table/limit days instead of
    # re-querying everything daily (2.6k RSS hits/day would eat half the
    # private-repo Actions quota). Indexed for the ORDER BY.
    news_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # When extract-funding-website last attempted this company (stamped on
    # every attempt, including "no funding found on site"). Without it,
    # companies whose sites never state funding stay eligible forever and the
    # same alphabetical head gets re-LLM'd every run. Indexed for the
    # WHERE/ORDER BY in the gap-fill query.
    website_funding_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # M3 — how this company first entered the DB.
    # 'vc_portfolio' | 'news' | 'techcrunch'. Discovery paths always set this
    # explicitly; the 'unknown' default is a safe fallback for any other insert
    # (replaces the old 'form_d' default removed when Form D ingestion was cut).
    discovered_via: Mapped[str] = mapped_column(
        String, nullable=False, server_default="unknown"
    )

    # Lifecycle status — 'active' | 'acquired' | 'shut_down' | 'ipo' (CHECK in
    # __table_args__). Set by extract-funding when an article or the company's
    # own site explicitly announces the event with medium/high confidence; a
    # non-active value is never overwritten by the pipeline (manual correction
    # is the escape hatch). status_source_url records the announcing article/
    # page, per the every-fact-has-a-source rule.
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default="active"
    )
    status_source_url: Mapped[str | None] = mapped_column(String, nullable=True)


class RawPage(Base):
    """Scraped-page cache for homepage / about / product pages.

    Per spec §4.8 + §5.3. Unique on (company_id, url) so the scraper
    can idempotently overwrite content+fetched_at with ON CONFLICT.

    ``content`` holds the *extracted visible text* of the page (capped at
    ~50k chars by scrape-homepages), not raw HTML — raw HTML at backlog
    scale would exceed Supabase's 500MB free tier. All consumers run
    extract_visible_text over it anyway, which is a no-op on plain text.
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
    """Per spec §4.3. A funding round attributed to a company, sourced from a
    news article (primary_news_url).
    """

    __tablename__ = "funding_rounds"
    __table_args__ = (
        CheckConstraint(
            "extraction_confidence IN ('low', 'medium', 'high') "
            "OR extraction_confidence IS NULL",
            name="ck_funding_rounds_extraction_confidence",
        ),
    )

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
    # URL slug for /investor/[slug]. Backfilled in migration 0018 and assigned
    # at insert time by upsert_investor; unique so the route is unambiguous.
    slug: Mapped[str] = mapped_column(unique=True, index=True)
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


class Competitor(Base):
    """Ranked competitor entry for a company, produced by the M4 analyze-competitors stage.

    Replace-style writes: each monthly run for a company DELETEs existing rows
    for that company_id then INSERTs the new ranked set inside one transaction.
    """

    __tablename__ = "competitors"

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable: many competitors won't match a row in our DB. Resolved via
    # exact normalized_name lookup in the stage.
    competitor_company_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    competitor_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(String, nullable=True)
    rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # Provenance: 'techcrunch' (named/implied in the company's TechCrunch
    # coverage) or 'llm_inferred' (general-knowledge competitor, shown as a
    # "potential" competitor in the UI). source_url is the TechCrunch article
    # when source='techcrunch', else NULL.
    source: Mapped[str] = mapped_column(
        String, nullable=False, server_default="llm_inferred"
    )
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "rank", name="uq_competitors_company_rank"),
    )


class Person(Base):
    """A leadership/founder entry for a company, extracted from the company's
    scraped website during the enrich-companies stage.

    Replace-style writes: each enrichment run for a company DELETEs existing
    rows for that company_id then INSERTs the new ranked set. Distinct from the
    (removed) Form D ``related_persons`` — these come from the company website.
    """

    __tablename__ = "people"

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    # Attribution — the company website the person was extracted from.
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "rank", name="uq_people_company_rank"),
    )


class CompanyInvestor(Base):
    """Company-level investor link. Distinct from
    :class:`FundingRoundInvestor`, which ties an investor to a specific
    funding round; this records that an investor is in a company at all,
    regardless of which round. Populated by the refresh-vc-portfolios stage:
    the firm whose portfolio surfaced a company IS an investor in it.

    Unique on the (company, investor) pair so a re-run of the stage can't
    double-link the same firm to the same company. ``is_lead`` is kept for
    symmetry with the round-level table (a portfolio page rarely tells us who
    led, so it defaults False).
    """

    __tablename__ = "company_investors"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "investor_id",
            name="uq_company_investors_company_investor",
        ),
    )

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
    )
    investor_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("investors.id", ondelete="CASCADE"),
        index=True,
    )
    # How we learned of the investment, e.g. 'vc_portfolio'.
    source: Mapped[str] = mapped_column(String, nullable=False)
    is_lead: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
