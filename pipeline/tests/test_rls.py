"""Every public table must have Row-Level Security enabled.

The web reads with the Supabase service_role key and the pipeline connects as
the table owner — both bypass RLS — so RLS-with-no-policies is pure
defense-in-depth: it denies every row to the anon/authenticated PostgREST roles.
0027 enabled it for the original tables, but 0040 (career_moves) and 0043
(fact_verifications) shipped without it until 0047. This guard makes the next
new table fail CI instead of silently exposing itself.

DB-gated: requires DATABASE_URL with the schema at ``alembic upgrade head``.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

# alembic_version holds only the schema revision id; Alembic owns it.
_EXEMPT = {"alembic_version"}


async def test_every_public_table_has_rls_enabled(db: AsyncSession) -> None:
    rows = await db.execute(
        text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "AND NOT c.relrowsecurity ORDER BY c.relname"
        )
    )
    missing = [name for (name,) in rows if name not in _EXEMPT]
    assert missing == [], (
        f"public tables without RLS: {missing} — add "
        "'ALTER TABLE <t> ENABLE ROW LEVEL SECURITY' to the migration that "
        "creates them (see 0027/0047)."
    )
