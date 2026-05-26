"""M2 schema: raw_pages table + companies enrichment columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-26 00:00:00.000000

Adds four enrichment columns to companies and creates the raw_pages table
for the homepage/about HTML cache per spec §4.1, §4.8, §5.2, §5.4.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- companies: M2 enrichment columns ---
    op.add_column("companies", sa.Column("primary_category", sa.String(), nullable=True))
    op.add_column(
        "companies",
        sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column(
            "last_enriched_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "companies",
        sa.Column("website_resolved_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- raw_pages ---
    op.create_table(
        "raw_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
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
            name="fk_raw_pages_company_id",
        ),
        sa.UniqueConstraint("company_id", "url", name="uq_raw_pages_company_url"),
    )
    op.create_index("ix_raw_pages_company_id", "raw_pages", ["company_id"])


def downgrade() -> None:
    op.drop_table("raw_pages")
    op.drop_column("companies", "website_resolved_at")
    op.drop_column("companies", "last_enriched_payload")
    op.drop_column("companies", "tags")
    op.drop_column("companies", "primary_category")
