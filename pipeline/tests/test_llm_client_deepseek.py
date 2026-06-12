"""Tests for the DeepSeek backend of nous.llm.client.complete_json.

All HTTP calls are mocked via httpx.MockTransport. Network is never touched.

Shared helpers (build_chat_response, ResponsePlan, patch_async_client) and the
autouse ledger-reset fixture live in conftest.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from nous.llm.client import (
    LLMError,
    LLMParseError,
    LLMRateLimitError,
    complete_json,
    get_ledger,
)
from nous.llm.prompts.company_description import CompanyDescription
from tests.conftest import ResponsePlan, build_chat_response, patch_async_client

VALID_PAYLOAD: dict[str, Any] = {
    "description_short": "A short description.",
    "description_long": "A longer description with detail.",
    "primary_category": "developer tools",
    "tags": ["api", "saas"],
    "website_state": "ok",
}

INVALID_PAYLOAD: dict[str, Any] = {"description_long": "missing required fields"}


@pytest.fixture(autouse=True)
def _set_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a dummy key so the client doesn't bail before the mocked call."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key-do-not-use-in-prod")


async def test_deepseek_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = ResponsePlan([(200, build_chat_response(VALID_PAYLOAD))])
    patch_async_client(monkeypatch, plan)

    result = await complete_json("some prompt", CompanyDescription)

    assert isinstance(result, CompanyDescription)
    assert result.description_short == "A short description."
    assert plan.call_count == 1


async def test_deepseek_retries_on_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = ResponsePlan(
        [
            (200, build_chat_response(INVALID_PAYLOAD)),
            (200, build_chat_response(VALID_PAYLOAD)),
        ]
    )
    patch_async_client(monkeypatch, plan)

    result = await complete_json("some prompt", CompanyDescription)
    assert isinstance(result, CompanyDescription)
    assert plan.call_count == 2


async def test_deepseek_raises_parse_error_after_two_bad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ResponsePlan(
        [
            (200, build_chat_response(INVALID_PAYLOAD)),
            (200, build_chat_response(INVALID_PAYLOAD)),
        ]
    )
    patch_async_client(monkeypatch, plan)

    with pytest.raises(LLMParseError):
        await complete_json("some prompt", CompanyDescription)
    assert plan.call_count == 2


async def test_deepseek_rate_limit_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = ResponsePlan([(429, {"error": "rate limited"})])
    patch_async_client(monkeypatch, plan)

    with pytest.raises(LLMRateLimitError):
        await complete_json("some prompt", CompanyDescription)


async def test_deepseek_5xx_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ResponsePlan(
        [
            (503, {"error": "overloaded"}),
            (503, {"error": "overloaded"}),
            (200, build_chat_response(VALID_PAYLOAD)),
        ]
    )
    patch_async_client(monkeypatch, plan)

    result = await complete_json("some prompt", CompanyDescription)
    assert isinstance(result, CompanyDescription)
    assert plan.call_count == 3


async def test_deepseek_4xx_non_429_raises_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ResponsePlan([(401, {"error": "unauthorized"})])
    patch_async_client(monkeypatch, plan)

    with pytest.raises(LLMError) as excinfo:
        await complete_json("some prompt", CompanyDescription)
    assert "401" in str(excinfo.value)
    # 401 is not retryable
    assert plan.call_count == 1


async def test_deepseek_missing_api_key_raises_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY is not set"):
        await complete_json("some prompt", CompanyDescription)


async def test_deepseek_malformed_response_raises_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with a body that doesn't have choices[0].message.content is surfaced clearly."""
    plan = ResponsePlan([(200, {"unexpected": "shape"})])
    patch_async_client(monkeypatch, plan)

    with pytest.raises(LLMError, match="missing choices"):
        await complete_json("some prompt", CompanyDescription)


# ---------------------------------------------------------------------------
# Ledger integration tests
# ---------------------------------------------------------------------------


async def test_ledger_increments_on_happy_path_with_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful call with a usage block increments calls and tokens."""
    plan = ResponsePlan(
        [(200, build_chat_response(
            VALID_PAYLOAD, include_usage=True, prompt_tokens=20, completion_tokens=8,
        ))]
    )
    patch_async_client(monkeypatch, plan)

    await complete_json("some prompt", CompanyDescription)

    ledger = get_ledger()
    assert ledger.calls == 1
    assert ledger.prompt_tokens == 20
    assert ledger.completion_tokens == 8
    assert ledger.parse_retries == 0


async def test_ledger_call_counted_even_without_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Responses without a usage key still count the call; tokens stay 0."""
    plan = ResponsePlan([(200, build_chat_response(VALID_PAYLOAD))])
    patch_async_client(monkeypatch, plan)

    await complete_json("some prompt", CompanyDescription)

    ledger = get_ledger()
    assert ledger.calls == 1
    assert ledger.prompt_tokens == 0
    assert ledger.completion_tokens == 0


async def test_wedged_call_hits_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call whose response never arrives fails at the overall deadline.

    Production run 27425089917 lost 59 minutes to a single wedged request:
    httpx's read timeout resets on every received fragment, so a stalled
    server that drips bytes (or a hung handshake path) can extend a call
    indefinitely. The overall asyncio deadline converts that into a bounded
    LLMError so the stage skips one company instead of losing its hour.
    """
    import asyncio

    import httpx as _httpx

    async def _never_responds(
        request: _httpx.Request,
    ) -> _httpx.Response:  # pragma: no cover - body intentionally unreachable
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    transport = _httpx.MockTransport(_never_responds)
    original_async_client = _httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> _httpx.AsyncClient:
        kwargs.pop("transport", None)
        return original_async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("nous.llm.client.httpx.AsyncClient", factory)
    monkeypatch.setattr("nous.llm.client._CALL_DEADLINE_SECONDS", 0.2)

    with pytest.raises(LLMError, match="deadline"):
        await asyncio.wait_for(complete_json("prompt", CompanyDescription), timeout=5)
