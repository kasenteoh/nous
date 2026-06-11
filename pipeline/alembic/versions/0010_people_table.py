"""Add people table (website-sourced leadership/founders).

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-11 01:00:00.000000

Leadership + founders extracted from the company's scraped website during the
enrich-companies stage. Replace-style writes (delete + insert per company),
unique on (company_id, rank) like the competitors table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=True),
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
            name="fk_people_company_id",
        ),
        sa.UniqueConstraint("company_id", "rank", name="uq_people_company_rank"),
    )
    op.create_index("ix_people_company_id", "people", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_people_company_id", table_name="people")
    op.drop_table("people")
