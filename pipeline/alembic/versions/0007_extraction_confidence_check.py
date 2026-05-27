"""Constrain funding_rounds.extraction_confidence to ('low','medium','high', NULL).

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-27 16:30:00.000000

Pydantic validates the LLM output at the boundary, but a downstream typo
or hand-INSERT could write "medum" and silently degrade _CONFIDENCE_RANK
comparisons in reconcile_funding_round. CHECK turns that into a loud
parse failure at the DB layer.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_funding_rounds_extraction_confidence",
        "funding_rounds",
        "extraction_confidence IN ('low', 'medium', 'high') "
        "OR extraction_confidence IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_funding_rounds_extraction_confidence",
        "funding_rounds",
        type_="check",
    )
