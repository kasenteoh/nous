"""Backfill companies.normalized_name to the new whitespace-collapsed form.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-27 12:00:00.000000

The normalize_name() helper used to collapse non-alphanumeric runs to a single
space. This split "OpenAI" (key "openai") from "Open AI Inc" (key "open ai")
into two rows because their pg_trgm similarity is ~0.5, below the 0.85 cutoff.
The helper now strips internal whitespace too — recompute existing rows so the
new exact-match path can find them.

The same regex-strip happens to be idempotent: running it twice yields the
same result.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE companies "
        "SET normalized_name = REGEXP_REPLACE(normalized_name, '[^a-z0-9]', '', 'g') "
        "WHERE normalized_name ~ '[^a-z0-9]'"
    )


def downgrade() -> None:
    # No-op: the old form is unrecoverable from the collapsed form
    # (we don't know where the spaces went). Re-running ingest stages will
    # repopulate normalized_name with whatever the current helper produces.
    pass
