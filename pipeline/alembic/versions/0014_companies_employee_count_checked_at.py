"""Add companies.employee_count_checked_at for estimate-employees back-off.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-11 12:05:00.000000

Records when the estimate-employees stage last attempted a company (success
or not) so the eligibility query can skip companies checked within the
refetch window. Without this, companies with no findable employee count would
be re-probed against every source on every weekly run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "employee_count_checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_companies_employee_count_checked_at",
        "companies",
        ["employee_count_checked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_companies_employee_count_checked_at",
        table_name="companies",
    )
    op.drop_column("companies", "employee_count_checked_at")
