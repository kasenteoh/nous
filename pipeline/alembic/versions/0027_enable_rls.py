"""Enable Row-Level Security on all public tables (defense-in-depth).

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-13 00:00:00.000000

The web app reads via the Supabase service_role key (bypasses RLS) and the
pipeline connects as the table owner (bypasses RLS). Enabling RLS with NO
policies therefore changes nothing for those two principals, but denies all
rows to the anon/authenticated PostgREST roles — so an exposed anon key or an
accidental public PostgREST path leaks nothing. Table names are a hardcoded
constant (not user input).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = (
    "companies",
    "raw_pages",
    "news_articles",
    "funding_rounds",
    "investors",
    "funding_round_investors",
    "competitors",
    "people",
    "company_investors",
    "company_snapshots",
    "company_relationships",
    "pipeline_runs",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
