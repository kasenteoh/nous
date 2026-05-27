"""M4 schema: competitors table

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-26 22:00:00.000000

Adds the M4 surface:
- competitors table (one row per (company, ranked competitor))
- UNIQUE (company_id, rank) so the replace-style write can't violate ordering
- indexes on company_id (primary access path) and competitor_company_id
  (future reverse-lookup view)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "competitors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "competitor_company_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("competitor_name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("reasoning", sa.String(), nullable=True),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
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
            name="fk_competitors_company_id",
        ),
        sa.ForeignKeyConstraint(
            ["competitor_company_id"],
            ["companies.id"],
            ondelete="SET NULL",
            name="fk_competitors_competitor_company_id",
        ),
        sa.UniqueConstraint(
            "company_id", "rank", name="uq_competitors_company_rank"
        ),
    )
    op.create_index("ix_competitors_company_id", "competitors", ["company_id"])
    op.create_index(
        "ix_competitors_competitor_company_id",
        "competitors",
        ["competitor_company_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_competitors_competitor_company_id", table_name="competitors")
    op.drop_index("ix_competitors_company_id", table_name="competitors")
    op.drop_table("competitors")
