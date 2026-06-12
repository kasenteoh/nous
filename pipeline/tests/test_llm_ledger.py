"""Tests for the LLM usage ledger in nous.llm.client.

All HTTP calls are mocked via httpx.MockTransport — network is never touched.
Tests cover: accumulation, missing usage key, reset, retry counting, and
estimated_cost_usd math.

Shared helpers (build_chat_response, ResponsePlan, patch_async_client) and
the autouse ledger-reset fixture live in conftest.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from nous.llm.client import (
    LLMParseError,
    complete_json,
    get_ledger,
    reset_ledger,
)
from nous.llm.prompts.company_description import CompanyDescription
from tests.conftest import ResponsePlan, build_chat_response, patch_async_client

# ---------------------------------------------------------------------------
# Test-local payload fixtures
# ---------------------------------------------------------------------------

VALID_PAYLOAD: dict[str, Any] = {
    "description_short": "A short description.",
    "description_long": "A longer description with detail.",
    "primary_category": "developer tools",
    "tags": ["api", "saas"],
}

INVALID_PAYLOAD: dict[str, Any] = {"description_long": "missing required fields"}


@pytest.fixture(autouse=True)
def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-ledger")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_usage_with_usage_field_accumulates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Responses that include usage accumulate calls and tokens."""
    plan = ResponsePlan(
        [
            (200, build_chat_response(VALID_PAYLOAD, include_usage=True,
                                      prompt_tokens=10, completion_tokens=5)),
            (200, build_chat_response(VALID_PAYLOAD, include_usage=True,
                                      prompt_tokens=20, completion_tokens=8)),
        ]
    )
    patch_async_client(monkeypatch, plan)

    await complete_json("p1", CompanyDescription)
    await complete_json("p2", CompanyDescription)

    ledger = get_ledger()
    assert ledger.calls == 2
    assert ledger.prompt_tokens == 30
    assert ledger.completion_tokens == 13
    assert ledger.parse_retries == 0


async def test_usage_without_usage_field_still_counts_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response with no `usage` key must still count as a call (tokens stay 0)."""
    plan = ResponsePlan([(200, build_chat_response(VALID_PAYLOAD, include_usage=False))])
    patch_async_client(monkeypatch, plan)

    await complete_json("prompt", CompanyDescription)

    ledger = get_ledger()
    assert ledger.calls == 1
    assert ledger.prompt_tokens == 0
    assert ledger.completion_tokens == 0


async def test_reset_ledger_zeroes_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset_ledger() restores all counters to zero."""
    plan = ResponsePlan([(200, build_chat_response(VALID_PAYLOAD, include_usage=True,
                                                    prompt_tokens=99))])
    patch_async_client(monkeypatch, plan)

    await complete_json("prompt", CompanyDescription)
    assert get_ledger().calls == 1

    reset_ledger()
    ledger = get_ledger()
    assert ledger.calls == 0
    assert ledger.prompt_tokens == 0
    assert ledger.completion_tokens == 0
    assert ledger.parse_retries == 0


async def test_get_ledger_returns_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutating the returned ledger must not affect the module-level one."""
    plan = ResponsePlan([(200, build_chat_response(VALID_PAYLOAD, include_usage=True,
                                                    prompt_tokens=5))])
    patch_async_client(monkeypatch, plan)

    await complete_json("prompt", CompanyDescription)
    snapshot = get_ledger()
    snapshot.calls = 999  # mutate the copy

    assert get_ledger().calls == 1  # module-level ledger unchanged


async def test_validation_failure_first_succeeds_second_counts_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First attempt fails validation, second attempt succeeds.

    parse_retries must be 1 because the retry was *issued* when the first
    attempt failed — regardless of whether the retry itself validates.
    This test would FAIL under the old semantic that only counted parse_retries
    when the *second* attempt also failed.

    Expected ledger: calls=2, parse_retries=1.
    """
    plan = ResponsePlan(
        [
            (200, build_chat_response(INVALID_PAYLOAD, include_usage=True,
                                      prompt_tokens=10, completion_tokens=4)),
            (200, build_chat_response(VALID_PAYLOAD, include_usage=True,
                                      prompt_tokens=10, completion_tokens=4)),
        ]
    )
    patch_async_client(monkeypatch, plan)

    result = await complete_json("prompt", CompanyDescription)

    assert result is not None
    ledger = get_ledger()
    assert ledger.calls == 2
    assert ledger.parse_retries == 1


async def test_validation_failure_retry_counts_two_calls_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First attempt returns invalid JSON → retry → still fails → LLMParseError.

    Under the new semantic, parse_retries counts retries ISSUED (i.e. when the
    first attempt's validation fails), so both-fail still yields parse_retries=1.

    Expected ledger: calls=2, parse_retries=1.
    """
    plan = ResponsePlan(
        [
            (200, build_chat_response(INVALID_PAYLOAD, include_usage=True,
                                      prompt_tokens=10, completion_tokens=4)),
            (200, build_chat_response(INVALID_PAYLOAD, include_usage=True,
                                      prompt_tokens=10, completion_tokens=4)),
        ]
    )
    patch_async_client(monkeypatch, plan)

    with pytest.raises(LLMParseError):
        await complete_json("prompt", CompanyDescription)

    ledger = get_ledger()
    assert ledger.calls == 2
    assert ledger.parse_retries == 1


async def test_estimated_cost_usd_math(monkeypatch: pytest.MonkeyPatch) -> None:
    """estimated_cost_usd matches the documented per-token rates."""
    from nous.llm.client import DEEPSEEK_USD_PER_MTOK_INPUT, DEEPSEEK_USD_PER_MTOK_OUTPUT

    plan = ResponsePlan(
        [(200, build_chat_response(
            VALID_PAYLOAD, include_usage=True,
            prompt_tokens=1_000_000, completion_tokens=1_000_000,
        ))]
    )
    patch_async_client(monkeypatch, plan)

    await complete_json("prompt", CompanyDescription)

    ledger = get_ledger()
    expected = DEEPSEEK_USD_PER_MTOK_INPUT + DEEPSEEK_USD_PER_MTOK_OUTPUT
    assert abs(ledger.estimated_cost_usd - expected) < 1e-9
