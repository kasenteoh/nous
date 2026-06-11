"""Remove SEC Form D ingestion: purge Form-D companies + drop filing schema.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-11 00:00:00.000000

The project no longer pulls companies from SEC Form D filings. This migration:

1. Purges every company whose sole provenance is Form D
   (``discovered_via = 'form_d'``). The CASCADE foreign keys clean up their
   filings, related_persons, funding_rounds, competitors, raw_pages, and
   news_articles automatically.
2. Drops ``funding_rounds.filing_id`` (the only remaining link into filings).
3. Drops the ``related_persons`` and ``filings`` tables.
4. Drops ``companies.cik`` (SEC Central Index Key — meaningless without EDGAR).
5. Removes the ``'form_d'`` server-default on ``companies.discovered_via``;
   all remaining sources (vc_portfolio, news, techcrunch) set it explicitly.

The purge in step 1 is destructive and irreversible — ``downgrade`` rebuilds
the schema but cannot resurrect deleted rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Purge Form-D-discovered companies. CASCADE FKs remove dependent rows.
    op.execute("DELETE FROM companies WHERE discovered_via = 'form_d'")

    # 2. Drop the funding_rounds → filings link.
    op.drop_constraint(
        "fk_funding_rounds_filing_id", "funding_rounds", type_="foreignkey"
    )
    op.drop_index("ix_funding_rounds_filing_id", table_name="funding_rounds")
    op.drop_column("funding_rounds", "filing_id")

    # 3. Drop Form D tables (related_persons references filings, so drop it first).
    op.drop_table("related_persons")
    op.drop_table("filings")

    # 4. Drop companies.cik and its unique constraint + index.
    op.drop_constraint("uq_companies_cik", "companies", type_="unique")
    op.drop_index("ix_companies_cik", table_name="companies")
    op.drop_column("companies", "cik")

    # 5. discovered_via no longer has a meaningful default now that Form D is gone.
    op.alter_column("companies", "discovered_via", server_default=None)


def downgrade() -> None:
    # NOTE: this rebuilds the schema but cannot restore the purged Form-D rows.

    # 5. Restore the discovered_via default.
    op.alter_column(
        "companies", "discovered_via", server_default=sa.text("'form_d'")
    )

    # 4. Recreate companies.cik.
    op.add_column("companies", sa.Column("cik", sa.String(), nullable=True))
    op.create_index("ix_companies_cik", "companies", ["cik"])
    op.create_unique_constraint("uq_companies_cik", "companies", ["cik"])

    # 3. Recreate filings + related_persons.
    op.create_table(
        "filings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accession_number", sa.String(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("offering_amount_total", sa.Numeric(20, 2), nullable=True),
        sa.Column("amount_sold", sa.Numeric(20, 2), nullable=True),
        sa.Column("investors_count", sa.Integer(), nullable=True),
        sa.Column("minimum_investment", sa.Numeric(20, 2), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            name="fk_filings_company_id",
        ),
        sa.UniqueConstraint("accession_number", name="uq_filings_accession_number"),
    )
    op.create_index("ix_filings_company_id", "filings", ["company_id"])
    op.create_index("ix_filings_accession_number", "filings", ["accession_number"])

    op.create_table(
        "related_persons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filing_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("relationship", sa.String(), nullable=False),
        sa.Column("address", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            name="fk_related_persons_company_id",
        ),
        sa.ForeignKeyConstraint(
            ["filing_id"],
            ["filings.id"],
            ondelete="CASCADE",
            name="fk_related_persons_filing_id",
        ),
    )
    op.create_index("ix_related_persons_company_id", "related_persons", ["company_id"])
    op.create_index("ix_related_persons_filing_id", "related_persons", ["filing_id"])

    # 2. Recreate funding_rounds.filing_id with the SET NULL FK (per 0006).
    op.add_column(
        "funding_rounds",
        sa.Column("filing_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_funding_rounds_filing_id", "funding_rounds", ["filing_id"])
    op.create_foreign_key(
        "fk_funding_rounds_filing_id",
        "funding_rounds",
        "filings",
        ["filing_id"],
        ["id"],
        ondelete="SET NULL",
    )
