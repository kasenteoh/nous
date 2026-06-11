"""Restore a safe server-default on companies.discovered_via.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-11 03:00:00.000000

Migration 0009 dropped the (now-misleading) 'form_d' default on
companies.discovered_via, leaving the NOT NULL column with no default. The
discovery paths (auto_create_company) always set it explicitly, but any other
insert — notably test fixtures — then failed the NOT NULL constraint. Restore a
neutral 'unknown' default so the column is robust without re-introducing a
source that no longer exists.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("companies", "discovered_via", server_default="unknown")


def downgrade() -> None:
    op.alter_column("companies", "discovered_via", server_default=None)
