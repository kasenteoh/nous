"""Shared fixtures for DB-gated integration tests and unit helpers.

The `db` fixture yields a session whose `commit()` commits a SAVEPOINT
inside an outer transaction. The outer transaction is rolled back at
teardown, so committed work inside the test (or inside the code under test)
is undone — perfect isolation across tests even when pipeline stages
commit per-row inside their loops.

Tests gate themselves with `pytestmark = pytest.mark.skipif(not DATABASE_URL, ...)`
at module scope. When DATABASE_URL is unset, the DB fixtures never run.

Also provides project-wide fixtures:
- reset_ledger_fixture: autouse, isolates the LLM usage ledger between tests.
- build_chat_response / ResponsePlan / patch_async_client: shared helpers for
  mocking the DeepSeek HTTP layer (used by test_llm_client_deepseek.py and
  test_llm_ledger.py).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Generator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ---------------------------------------------------------------------------
# LLM ledger isolation — autouse so every test starts with a clean slate
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_ledger() -> Generator[None, None, None]:
    """Reset the module-level LLM usage ledger before and after each test."""
    from nous.llm.client import reset_ledger

    reset_ledger()
    yield
    reset_ledger()


@pytest.fixture(autouse=True)
def _reset_domain_throttle() -> Generator[None, None, None]:
    """Reset the process-wide per-domain throttle registry around each test.

    The default registry is deliberately shared across client instances (that
    sharing is the W-C.1 fix); without a reset, a test that hits example.com
    would make the next test's example.com fetch wait out the interval.
    """
    from nous.sources._http import DEFAULT_THROTTLE

    DEFAULT_THROTTLE.reset()
    yield
    DEFAULT_THROTTLE.reset()


# ---------------------------------------------------------------------------
# Shared DeepSeek HTTP-mock helpers
# ---------------------------------------------------------------------------


def build_chat_response(
    payload: dict[str, Any],
    *,
    include_usage: bool = False,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> dict[str, Any]:
    """Build a minimal DeepSeek-shaped /chat/completions response.

    Pass ``include_usage=True`` to attach a real usage block; the default
    omits it so tests that pre-date the ledger remain unaffected.
    """
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": "stop",
            }
        ],
    }
    if include_usage:
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return body


class ResponsePlan:
    """Routes successive POSTs through a queue of (status, body) tuples."""

    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        if not self._responses:
            raise AssertionError(
                f"unexpected extra call #{self.call_count} to {request.url}"
            )
        status, body = self._responses.pop(0)
        content = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return httpx.Response(
            status,
            content=content,
            headers={"content-type": "application/json"},
            request=request,
        )


def patch_async_client(monkeypatch: pytest.MonkeyPatch, plan: ResponsePlan) -> None:
    """Replace httpx.AsyncClient inside llm/client with one bound to a MockTransport."""
    transport = httpx.MockTransport(plan.handler)
    original_async_client = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return original_async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("nous.llm.client.httpx.AsyncClient", factory)


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


@pytest_asyncio.fixture()
async def committed_session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session FACTORY on a single isolated connection, for testing that a
    stage's writes PERSIST ACROSS sessions (i.e. it commits, not just flushes).

    All sessions built from this factory share one connection whose outer
    transaction is rolled back at teardown. With join_transaction_mode=
    "create_savepoint", a session's commit() RELEASES its savepoint into that
    shared transaction (so a later, separate session on this connection sees the
    data), while a session that only flush()es has its savepoint ROLLED BACK
    when it closes (so a later session does NOT see it). That difference is
    exactly what the single-shared-session `db` fixture cannot detect — it makes
    a flush visible to its own assertions and so passes even when prod would
    roll the write back.

    Usage: open one session to set up + commit fixtures, a SECOND to run the
    stage (like the CLI does), and a THIRD to verify the writes are visible.
    """
    async with engine.connect() as connection:
        outer_tx = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield factory
        finally:
            if outer_tx.is_active:
                await outer_tx.rollback()
