"""M3 schema: discovered_via + news_articles + funding_rounds + investors + pg_trgm

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-27 02:00:00.000000

Adds the M3 surface:
- companies.discovered_via column (defaults to 'form_d'; existing rows
  backfill via the server_default on the ADD COLUMN)
- news_articles, funding_rounds, investors, funding_round_investors tables
- pg_trgm extension + GIN trigram index on companies.normalized_name to
  support the fuzzy-match path used by auto-create (M3 Chunk 5)
- Partial index on news_articles for the unprocessed work queue
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- pg_trgm extension (required for fuzzy company-name matching) ---
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # --- companies.discovered_via (backfills existing rows via server_default) ---
    op.add_column(
        "companies",
        sa.Column(
            "discovered_via",
            sa.String(),
            nullable=False,
            server_default="form_d",
        ),
    )

    # GIN trigram index on normalized_name — supports fuzzy match in
    # find_company_by_name (M3 Chunk 5). The `gin_trgm_ops` operator class
    # is provided by pg_trgm; the index serves `similarity()` and `%` queries.
    op.execute(
        "CREATE INDEX ix_companies_normalized_name_trgm "
        "ON companies USING gin (normalized_name gin_trgm_ops)"
    )

    # --- news_articles ---
    op.create_table(
        "news_articles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.Column("raw_content", sa.String(), nullable=False),
        sa.Column(
            "processed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
            name="fk_news_articles_company_id",
        ),
        sa.UniqueConstraint("url", name="uq_news_articles_url"),
    )
    op.create_index("ix_news_articles_company_id", "news_articles", ["company_id"])
    op.create_index("ix_news_articles_url", "news_articles", ["url"], unique=True)
    op.create_index("ix_news_articles_processed", "news_articles", ["processed"])
    # Partial index: the extract-funding work queue scans `WHERE processed=false`,
    # ordered by created_at — this index covers that path narrowly.
    op.execute(
        "CREATE INDEX ix_news_articles_unprocessed "
        "ON news_articles (created_at) WHERE processed = false"
    )

    # --- funding_rounds ---
    op.create_table(
        "funding_rounds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round_type", sa.String(), nullable=True),
        sa.Column("amount_raised", sa.Numeric(20, 2), nullable=True),
        sa.Column("valuation_post_money", sa.Numeric(20, 2), nullable=True),
        sa.Column("valuation_source", sa.String(), nullable=True),
        sa.Column("announced_date", sa.Date(), nullable=True),
        sa.Column("filing_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("primary_news_url", sa.String(), nullable=True),
        sa.Column("extraction_confidence", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
            name="fk_funding_rounds_company_id",
        ),
        sa.ForeignKeyConstraint(
            ["filing_id"],
            ["filings.id"],
            name="fk_funding_rounds_filing_id",
        ),
    )
    op.create_index("ix_funding_rounds_company_id", "funding_rounds", ["company_id"])
    op.create_index("ix_funding_rounds_announced_date", "funding_rounds", ["announced_date"])
    op.create_index("ix_funding_rounds_filing_id", "funding_rounds", ["filing_id"])

    # --- investors ---
    op.create_table(
        "investors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_normalized", sa.String(), nullable=False),
        sa.Column(
            "type",
            sa.String(),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("website", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("name_normalized", name="uq_investors_name_normalized"),
    )
    op.create_index("ix_investors_name_normalized", "investors", ["name_normalized"], unique=True)

    # --- funding_round_investors (join) ---
    op.create_table(
        "funding_round_investors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("funding_round_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "is_lead",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["funding_round_id"],
            ["funding_rounds.id"],
            ondelete="CASCADE",
            name="fk_funding_round_investors_round_id",
        ),
        sa.ForeignKeyConstraint(
            ["investor_id"],
            ["investors.id"],
            ondelete="CASCADE",
            name="fk_funding_round_investors_investor_id",
        ),
        sa.UniqueConstraint(
            "funding_round_id",
            "investor_id",
            name="uq_funding_round_investors_round_investor",
        ),
    )
    op.create_index(
        "ix_funding_round_investors_round_id",
        "funding_round_investors",
        ["funding_round_id"],
    )
    op.create_index(
        "ix_funding_round_investors_investor_id",
        "funding_round_investors",
        ["investor_id"],
    )


def downgrade() -> None:
    op.drop_table("funding_round_investors")
    op.drop_table("investors")
    op.drop_table("funding_rounds")
    op.execute("DROP INDEX IF EXISTS ix_news_articles_unprocessed")
    op.drop_table("news_articles")
    op.execute("DROP INDEX IF EXISTS ix_companies_normalized_name_trgm")
    op.drop_column("companies", "discovered_via")
    # pg_trgm extension is intentionally not dropped on downgrade — it's a
    # cluster-wide resource that other schemas may depend on.
