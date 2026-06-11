"""company_investors table

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-11 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the company_investors join table.

    NOTE: autogenerate also emitted a batch of unrelated unique-constraint /
    index churn for pre-existing tables (companies, filings, investors,
    news_articles, funding_round_investors). That churn is spurious drift
    between the ORM metadata and the live DB's index/constraint *naming* — not
    a change introduced by this revision — so it has been dropped. This
    migration touches only ``company_investors``.
    """
    op.create_table(
        "company_investors",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("investor_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "is_lead", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column("id", sa.UUID(), nullable=False),
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
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["investor_id"], ["investors.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "investor_id",
            name="uq_company_investors_company_investor",
        ),
    )
    op.create_index(
        op.f("ix_company_investors_company_id"),
        "company_investors",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_company_investors_investor_id"),
        "company_investors",
        ["investor_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the company_investors join table."""
    op.drop_index(
        op.f("ix_company_investors_investor_id"),
        table_name="company_investors",
    )
    op.drop_index(
        op.f("ix_company_investors_company_id"),
        table_name="company_investors",
    )
    op.drop_table("company_investors")
