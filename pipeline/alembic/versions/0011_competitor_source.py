"""Add competitors.source + competitors.source_url for provenance.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-11 02:00:00.000000

Competitors are now attributed to either 'techcrunch' (named/implied in the
company's TechCrunch coverage) or 'llm_inferred' (general-knowledge competitor,
rendered as "potential"). source_url carries the TechCrunch article when
source='techcrunch'. Existing rows default to 'llm_inferred'.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "competitors",
        sa.Column(
            "source",
            sa.String(),
            nullable=False,
            server_default="llm_inferred",
        ),
    )
    op.add_column(
        "competitors",
        sa.Column("source_url", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("competitors", "source_url")
    op.drop_column("competitors", "source")
