from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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

    ``total_raised_usd`` is a STATED cumulative total from an article ("has
    raised $285M to date"), distinct from the sum of funding_rounds rows —
    news discovery's 7-day lookback means historical rounds are never
    backfilled, so the sum undercounts older companies. Every rendered fact
    needs a source, hence ``total_raised_source_url``; ``total_raised_as_of``
    (the stating article's published date) gives newest-article-wins
    semantics when claims conflict.
    """

    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'acquired', 'shut_down', 'ipo')",
            name="ck_companies_status",
        ),
        CheckConstraint(
            "exclusion_reason IN ('parse_artifact', 'non_us', 'not_a_startup', 'manual') "
            "OR exclusion_reason IS NULL",
            name="ck_companies_exclusion_reason",
        ),
        # GIN index for array containment / overlap on the /tag pages and the
        # tag filter (``tags @> ...``). A btree can't index an array for those
        # operators, hence the explicit GIN here rather than ``index=True`` on
        # the column. See migration 0030.
        Index("ix_companies_tags", "tags", postgresql_using="gin"),
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
    # Indexed: equality filter on the /location pages and the location filter.
    hq_state: Mapped[str | None] = mapped_column(index=True)
    # hq_country: NULL until evidenced. Do NOT set a Python-level default here;
    # the old default="US" caused every auto-created company to read as US even
    # when the company is foreign. Country is inferred from the website ccTLD or
    # an explicit LLM statement during enrich-companies / judge-eligibility.
    hq_country: Mapped[str | None]

    # Company metadata
    year_incorporated: Mapped[int | None]
    # Indexed: equality filter + industry facet on the /companies browse page.
    industry_group: Mapped[str | None] = mapped_column(index=True)

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

    # When infer-hq-country last attempted this company (success or not).
    # Drives that stage's selection (WHERE ... IS NULL), back-off, and
    # idempotency: a bounded dispatch-gated run drains the shown + hq_country
    # IS NULL backlog over successive dispatches, and a row that yielded no
    # country is stamped so it is not re-fetched. Mirrors the other *_checked_at
    # rotation stamps. Indexed for that WHERE clause.
    hq_country_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # M3 — how this company first entered the DB.
    # 'vc_portfolio' | 'news' | 'techcrunch'. Discovery paths always set this
    # explicitly; the 'unknown' default is a safe fallback for any other insert
    # (replaces the old 'form_d' default removed when Form D ingestion was cut).
    # Indexed: the discovery-source filter on the /companies browse page.
    discovered_via: Mapped[str] = mapped_column(
        String, nullable=False, server_default="unknown", index=True
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

    # Stated cumulative "total raised to date" — a figure an article (or the
    # company's own site) explicitly states, NOT a sum of funding_rounds rows.
    # Applied by extract-funding (_apply_total_raised) with newest-article-wins
    # semantics ordered by total_raised_as_of; total_raised_source_url records
    # the stating article/page, per the every-fact-has-a-source rule. All
    # three columns always travel together.
    total_raised_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True
    )
    total_raised_source_url: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    total_raised_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Catalog-quality soft exclusion (spec 2026-06-12). NULL = included.
    # 'parse_artifact' | 'non_us' | 'not_a_startup' | 'manual' (CHECK above).
    # Set by enrich-companies / judge-eligibility / repair-catalog / the
    # exclude-company CLI; NEVER cleared by discovery (re-appearing on a VC
    # portfolio page is not new evidence). Indexed: every catalog query and
    # every pipeline selection filters on IS NULL.
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    exclusion_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    excluded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # When the is-this-a-startup judgment last ran (enrich path or the
    # judge-eligibility backfill). Lets the backfill find enriched-but-unjudged
    # rows exactly once. Indexed for that WHERE.
    eligibility_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # URLs confirmed NOT to be this company's site (parked/for-sale or an
    # unrelated business) — resolve-homepages must never re-pick a domain in
    # here. JSONB list of strings; ALWAYS reassign (rejected_urls = [*old, new]),
    # never append in place — plain JSONB columns don't track mutation.
    rejected_urls: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # Denormalized count(funding_rounds) maintained by reconcile_funding_round
    # + merge_companies and backfilled in migration 0022. Exists so the web
    # catalog bar (description OR funded) is a flat indexed WHERE — PostgREST
    # can't paginate an OR over an EXISTS subquery.
    funding_round_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", index=True
    )

    # Denormalized "most recent funding round" fields (migration 0028),
    # maintained by the refresh-latest-round stage (called at the end of the
    # extract-funding paths). "Most recent" = the round with the greatest
    # announced_date (NULLS LAST). They exist so the web browse page can sort by
    # funding amount / recency without a cross-table aggregate — PostgREST can't
    # ORDER BY an aggregate over a one-to-many embed. amount/date are indexed
    # (they back the funding_desc / recently_funded sorts and the funded-since /
    # raise-range filters); latest_round_type is a small free-text label used by
    # the stage filter (eq), and is indexed because it appears in that WHERE.
    latest_round_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 2), nullable=True, index=True
    )
    latest_round_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, index=True
    )
    latest_round_type: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )


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
    # unique=True backs the uq_news_articles_url constraint (a unique index).
    # No separate index=True — that produced a redundant ix_news_articles_url
    # over the same column, dropped in migration 0020.
    url: Mapped[str] = mapped_column(unique=True)
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
    # Denormalized count of distinct non-excluded companies this investor backs,
    # via EITHER company_investors OR funding_round_investors → funding_rounds.
    # Maintained by refresh-investor-counts (called at the end of
    # refresh-vc-portfolios and extract-funding) and backfilled in migration
    # 0025. Indexed so the web investor index can ORDER BY portfolio_count DESC.
    portfolio_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", index=True
    )


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
        # A company can never be its own competitor. Enforced in the DB by
        # migration 0020; mirrored here so the model and schema agree.
        CheckConstraint(
            "competitor_company_id IS NULL OR competitor_company_id <> company_id",
            name="ck_competitors_no_self_reference",
        ),
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


class CompanySnapshot(Base):
    """A weekly time-series snapshot of a company's momentum signals.

    Written by the snapshot-companies stage: one row per company per ISO week,
    capturing the headcount range and trailing-30-day news volume as they stood
    at capture time. Wave-4 momentum charts read this table; it costs nothing to
    accumulate now and cannot be reconstructed retroactively, so we record
    early ("record first, render later").

    ``captured_week`` is the ISO-week Monday of capture. The UNIQUE
    (company_id, captured_week) is the idempotency key: a same-week re-run
    upserts the row in place rather than appending, so values stay fresh while
    the row count per company per week stays exactly one.

    Deliberately NO job_postings_count column — no writer exists for it yet;
    added when a stage produces the data, per the no-unattributed-fact rule.
    """

    __tablename__ = "company_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "captured_week",
            name="uq_company_snapshots_company_week",
        ),
    )

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # ISO-week Monday of capture. Part of the unique idempotency key, but that
    # composite UNIQUE leads with company_id and can't serve a captured_week-only
    # predicate, so index it standalone (the post-upsert count filters on it).
    # A --week backfill writes to the chosen week's Monday.
    captured_week: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Headcount range as it stood at capture time (mirrors Company.employee_count_*;
    # nullable because many companies have no findable headcount yet).
    employee_count_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    employee_count_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Count of news_articles for this company published in the trailing 30 days
    # at capture time. NOT NULL: a company with no recent news snapshots a 0,
    # which is a real signal (quiet week), not missing data.
    news_count_30d: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )


class CompanyRelationship(Base):
    """A typed, directed edge in the startup relationship graph.

    The unified company<->company edge table behind the "Related companies"
    surface. Unlike ``competitors`` (which keeps name-only, unresolved entries
    for its ranked-list UI), every row here has *both* endpoints resolved to
    companies in our DB — it is a clean internal graph.

    Populated set-based, replace-style, with zero LLM cost by the
    ``derive-relationships`` stage:
    - ``competitor`` edges are projected from resolved ``competitors`` rows;
    - ``similar`` edges come from shared ``industry_group`` + ``tags`` overlap.

    The ``supplier``/``customer``/``partner`` types are reserved in the CHECK so
    the schema is ready for a future (human-reviewed) LLM supply-chain pass; they
    are not populated today. "Also backed by" (shared-investor) edges are
    deliberately NOT stored here — a mega-investor would make them O(N^2); they
    are derived at read time in the web layer, capped.

    Directed storage (one row per ``company_id -> related_company_id``) keeps the
    dominant read — "everything related to company X" — a trivial
    ``WHERE company_id = X``. The derive stage writes both directions for
    symmetric types (``similar``).
    """

    __tablename__ = "company_relationships"

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    related_company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relationship_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Strength / ordering signal: tag-overlap score for 'similar', 1/rank for
    # 'competitor'. Higher = stronger; the UI orders by this descending.
    score: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    # Provenance (every rendered fact needs a source): 'competitors' |
    # 'industry_tags' | (reserved: 'llm_inferred').
    source: Mapped[str] = mapped_column(String, nullable=False)
    # Short human-readable justification shown as the edge's caption, e.g.
    # "Both in developer-tools; 4 shared tags".
    evidence: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "related_company_id",
            "relationship_type",
            name="uq_company_relationships_pair_type",
        ),
        # A company can never be related to itself (mirrors
        # ck_competitors_no_self_reference).
        CheckConstraint(
            "related_company_id <> company_id",
            name="ck_company_relationships_no_self",
        ),
        CheckConstraint(
            "relationship_type IN "
            "('competitor', 'similar', 'supplier', 'customer', 'partner')",
            name="ck_company_relationships_type",
        ),
    )


class PipelineRun(Base):
    """One row per pipeline-stage execution — the observability audit trail.

    Stages run as separate CLI processes under continue-on-error, so a stage can
    silently produce nothing (a flush-without-commit bug, a validator rejecting
    all LLM output, a blocked source) while the workflow stays green. This table
    records each run's input/output counts so a silent-empty-table surfaces
    immediately (status='empty') instead of waiting to be noticed, and gives a
    queryable history of what each stage did and when.
    """

    __tablename__ = "pipeline_runs"

    stage: Mapped[str] = mapped_column(String, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 'success' | 'empty' (processed inputs but wrote 0 rows — a silent-failure
    # signal for stages whose output should track input) | 'error'.
    status: Mapped[str] = mapped_column(String, nullable=False)
    inputs_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    rows_written: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    # The stage's full summary model (model_dump) for ad-hoc inspection.
    summary: Mapped[dict | None] = mapped_column(JSONB)  # type: ignore[type-arg]

    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'empty', 'error')",
            name="ck_pipeline_runs_status",
        ),
    )
