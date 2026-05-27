"""Tests for the DeepSeek backend of nous.llm.client.complete_json.

All HTTP calls are mocked via httpx.MockTransport. Network is never touched.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nous.llm.client import (
    LLMError,
    LLMParseError,
    LLMRateLimitError,
    complete_json,
)
from nous.llm.prompts.company_description import CompanyDescription

VALID_PAYLOAD: dict[str, Any] = {
    "description_short": "A short description.",
    "description_long": "A longer description with detail.",
    "primary_category": "developer tools",
    "tags": ["api", "saas"],
}

INVALID_PAYLOAD: dict[str, Any] = {"description_long": "missing required fields"}


def _build_chat_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape mirrors DeepSeek's OpenAI-compatible /chat/completions response."""
    return {
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


class _ResponsePlan:
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


@pytest.fixture(autouse=True)
def _force_deepseek_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file talks to the DeepSeek backend."""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key-do-not-use-in-prod")


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch, plan: _ResponsePlan
) -> None:
    """Replace httpx.AsyncClient inside llm/client with one bound to a MockTransport."""
    transport = httpx.MockTransport(plan.handler)
    original_async_client = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        # Drop the caller's transport if any; force ours.
        kwargs.pop("transport", None)
        return original_async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("nous.llm.client.httpx.AsyncClient", factory)


async def test_deepseek_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _ResponsePlan([(200, _build_chat_response(VALID_PAYLOAD))])
    _patch_async_client(monkeypatch, plan)

    result = await complete_json("some prompt", CompanyDescription)

    assert isinstance(result, CompanyDescription)
    assert result.description_short == "A short description."
    assert plan.call_count == 1


async def test_deepseek_retries_on_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _ResponsePlan(
        [
            (200, _build_chat_response(INVALID_PAYLOAD)),
            (200, _build_chat_response(VALID_PAYLOAD)),
        ]
    )
    _patch_async_client(monkeypatch, plan)

    result = await complete_json("some prompt", CompanyDescription)
    assert isinstance(result, CompanyDescription)
    assert plan.call_count == 2


async def test_deepseek_raises_parse_error_after_two_bad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _ResponsePlan(
        [
            (200, _build_chat_response(INVALID_PAYLOAD)),
            (200, _build_chat_response(INVALID_PAYLOAD)),
        ]
    )
    _patch_async_client(monkeypatch, plan)

    with pytest.raises(LLMParseError):
        await complete_json("some prompt", CompanyDescription)
    assert plan.call_count == 2


async def test_deepseek_rate_limit_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _ResponsePlan([(429, {"error": "rate limited"})])
    _patch_async_client(monkeypatch, plan)

    with pytest.raises(LLMRateLimitError):
        await complete_json("some prompt", CompanyDescription)


async def test_deepseek_5xx_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _ResponsePlan(
        [
            (503, {"error": "overloaded"}),
            (503, {"error": "overloaded"}),
            (200, _build_chat_response(VALID_PAYLOAD)),
        ]
    )
    _patch_async_client(monkeypatch, plan)

    result = await complete_json("some prompt", CompanyDescription)
    assert isinstance(result, CompanyDescription)
    assert plan.call_count == 3


async def test_deepseek_4xx_non_429_raises_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _ResponsePlan([(401, {"error": "unauthorized"})])
    _patch_async_client(monkeypatch, plan)

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
    plan = _ResponsePlan([(200, {"unexpected": "shape"})])
    _patch_async_client(monkeypatch, plan)

    with pytest.raises(LLMError, match="missing choices"):
        await complete_json("some prompt", CompanyDescription)
