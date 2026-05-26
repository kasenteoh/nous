"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-26 00:00:00.000000

Creates tables: companies, filings, related_persons.
Indexes per spec §4.9 (M1 scope): all FKs, all WHERE-candidate columns,
and UNIQUE constraints on companies.slug, companies.cik, filings.accession_number.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- companies ---
    op.create_table(
        "companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("cik", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("normalized_name", sa.String(), nullable=False),
        sa.Column("description_short", sa.String(), nullable=True),
        sa.Column("description_long", sa.String(), nullable=True),
        sa.Column("website", sa.String(), nullable=True),
        sa.Column("logo_url", sa.String(), nullable=True),
        sa.Column("hq_city", sa.String(), nullable=True),
        sa.Column("hq_state", sa.String(), nullable=True),
        sa.Column("hq_country", sa.String(), nullable=True),
        sa.Column("year_incorporated", sa.Integer(), nullable=True),
        sa.Column("industry_group", sa.String(), nullable=True),
        sa.Column("employee_count_min", sa.Integer(), nullable=True),
        sa.Column("employee_count_max", sa.Integer(), nullable=True),
        sa.Column("employee_count_source", sa.String(), nullable=True),
        sa.Column("last_enriched_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("cik", name="uq_companies_cik"),
        sa.UniqueConstraint("slug", name="uq_companies_slug"),
    )
    op.create_index("ix_companies_cik", "companies", ["cik"])
    op.create_index("ix_companies_slug", "companies", ["slug"])
    op.create_index("ix_companies_normalized_name", "companies", ["normalized_name"])

    # --- filings ---
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

    # --- related_persons ---
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


def downgrade() -> None:
    op.drop_table("related_persons")
    op.drop_table("filings")
    op.drop_table("companies")
