"""Set ondelete=SET NULL on funding_rounds.filing_id.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-27 16:00:00.000000

The FK previously had no ondelete clause (defaults to NO ACTION). A
FundingRound's news attribution stands on its own; losing the filing link
when a Filing is removed is acceptable.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_funding_rounds_filing_id",
        "funding_rounds",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_funding_rounds_filing_id",
        "funding_rounds",
        "filings",
        ["filing_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_funding_rounds_filing_id",
        "funding_rounds",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_funding_rounds_filing_id",
        "funding_rounds",
        "filings",
        ["filing_id"],
        ["id"],
    )
