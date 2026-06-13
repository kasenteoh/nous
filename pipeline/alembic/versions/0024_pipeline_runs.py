"""pipeline_runs observability table

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-13 00:00:00.000000

One row per pipeline-stage execution: stage, start/finish, status
(success/empty/error), inputs_seen, rows_written, error, and the full summary
JSONB. Lets a silent-empty-table (a stage that processed inputs but committed
nothing) surface as status='empty' instead of hiding behind a green run.

Hand-written per the 0015+ convention.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "inputs_seen", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "rows_written", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("summary", postgresql.JSONB(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('success', 'empty', 'error')",
            name="ck_pipeline_runs_status",
        ),
    )
    op.create_index("ix_pipeline_runs_stage", "pipeline_runs", ["stage"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_runs_stage", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
