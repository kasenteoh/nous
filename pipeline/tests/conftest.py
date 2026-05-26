"""Shared fixtures for DB-gated integration tests.

The `db` fixture yields a session whose `commit()` commits a SAVEPOINT
inside an outer transaction. The outer transaction is rolled back at
teardown, so committed work inside the test (or inside the code under test)
is undone — perfect isolation across tests even when pipeline stages
commit per-row inside their loops.

Tests gate themselves with `pytestmark = pytest.mark.skipif(not DATABASE_URL, ...)`
at module scope. When DATABASE_URL is unset, these fixtures never run.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set", allow_module_level=True)
    eng = create_async_engine(DATABASE_URL, echo=False)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture()
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Yield an isolated AsyncSession.

    A single outer transaction is held at the connection level. The session
    runs in `join_transaction_mode="create_savepoint"`, so any `session.commit()`
    inside the test or the code under test commits only a SAVEPOINT. Teardown
    rolls back the outer transaction, undoing all writes.
    """
    async with engine.connect() as connection:
        outer_tx = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            async with factory() as session:
                yield session
        finally:
            if outer_tx.is_active:
                await outer_tx.rollback()
