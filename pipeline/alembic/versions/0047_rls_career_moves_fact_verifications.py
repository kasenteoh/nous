"""Enable Row-Level Security on career_moves + fact_verifications

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-09 00:00:00.000000

Closes a defense-in-depth gap. 0027 enabled RLS (no policies) on every public
table that existed then, and 0032/0034 did the same for slug_aliases / themes /
company_themes — but 0040 (career_moves) and 0043 (fact_verifications) shipped
without it. On Supabase's default grants that leaves both tables readable AND
writable by the anon/authenticated PostgREST roles: anyone holding the anon key
could insert a ``verdict='supported'`` row, which the web renders as a public
"✓ Verified against source" badge — forging the provenance moat.

Same rationale as 0027: the web reads with the service_role key and the
pipeline connects as the table owner, both of which bypass RLS, so enabling it
with NO policies changes nothing for them and denies all rows to anon /
authenticated. ``tests/test_rls.py`` now asserts every public table has RLS on,
so the next new table can't repeat the miss.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES: tuple[str, ...] = ("career_moves", "fact_verifications")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
