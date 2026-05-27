"""Add companies.last_scrape_attempt_at for failed-fetch back-off.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-27 17:00:00.000000

Records the most recent scrape attempt (success or failure) so the
scrape-homepages eligibility query can exclude companies whose pages
failed recently. Without this, dead URLs (robots-block, 404, network
error) are re-attempted every weekly run because raw_pages stays empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "last_scrape_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_companies_last_scrape_attempt_at",
        "companies",
        ["last_scrape_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_companies_last_scrape_attempt_at",
        table_name="companies",
    )
    op.drop_column("companies", "last_scrape_attempt_at")
