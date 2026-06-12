"""Engine-level connection guards (see get_engine).

Production runs 27425089917 and 27436088686 each lost their enrich hour to a
single wedged Supabase pooler connection: the bare engine had no keepalives,
no pre-ping, and no statement timeout, so a silently dropped socket hung the
per-company commit until the workflow step timeout fired. These tests assert
the guards are actually applied to live connections.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


async def test_engine_applies_statement_timeout() -> None:
    from nous.db.session import get_engine

    engine = get_engine()
    async with engine.connect() as conn:
        value = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
    assert value == "1min"


async def test_engine_has_pre_ping_enabled() -> None:
    from nous.db.session import get_engine

    assert get_engine().pool._pre_ping is True  # type: ignore[attr-defined]
