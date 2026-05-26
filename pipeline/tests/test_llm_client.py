"""Tests for nous.llm.client — all network calls are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import errors as genai_errors

from nous.llm.client import (
    LLMError,
    LLMParseError,
    LLMRateLimitError,
    complete_json,
)
from nous.llm.prompts.company_description import CompanyDescription

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_JSON = """{
    "description_short": "A short description.",
    "description_long": "A longer description with detail.",
    "primary_category": "developer tools",
    "tags": ["api", "saas"]
}"""

INVALID_JSON = '{"description_long": "only long"}'  # missing required fields


def _make_response(text: str) -> MagicMock:
    """Build a fake generate_content response object."""
    mock = MagicMock()
    mock.text = text
    return mock


def _make_client_mock(side_effects: list[object]) -> MagicMock:
    """Return a fake genai.Client whose aio.models.generate_content yields side_effects."""
    client_mock = MagicMock()
    generate_mock = AsyncMock(side_effect=side_effects)
    client_mock.aio.models.generate_content = generate_mock
    return client_mock


# ---------------------------------------------------------------------------
# Happy path: valid JSON on first try
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_json_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """complete_json returns a validated CompanyDescription on success."""
    client_mock = _make_client_mock([_make_response(VALID_JSON)])

    with patch("nous.llm.client._build_client", return_value=client_mock):
        result = await complete_json("some prompt", CompanyDescription)

    assert isinstance(result, CompanyDescription)
    assert result.description_short == "A short description."
    assert result.primary_category == "developer tools"
    assert result.tags == ["api", "saas"]

    # LLM was called exactly once
    assert client_mock.aio.models.generate_content.call_count == 1


# ---------------------------------------------------------------------------
# Parse failure happy path: first call bad, second call good
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_json_retry_on_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the first response fails validation, complete_json retries once."""
    client_mock = _make_client_mock(
        [_make_response(INVALID_JSON), _make_response(VALID_JSON)]
    )

    with patch("nous.llm.client._build_client", return_value=client_mock):
        result = await complete_json("some prompt", CompanyDescription)

    assert isinstance(result, CompanyDescription)
    # LLM was called twice (first attempt returned bad JSON, second returned good)
    assert client_mock.aio.models.generate_content.call_count == 2


# ---------------------------------------------------------------------------
# Parse failure persistent: both calls return malformed JSON → LLMParseError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_json_parse_failure_persistent(monkeypatch: pytest.MonkeyPatch) -> None:
    """If both attempts return invalid JSON, raise LLMParseError."""
    client_mock = _make_client_mock(
        [_make_response(INVALID_JSON), _make_response(INVALID_JSON)]
    )

    with (
        patch("nous.llm.client._build_client", return_value=client_mock),
        pytest.raises(LLMParseError),
    ):
        await complete_json("some prompt", CompanyDescription)

    assert client_mock.aio.models.generate_content.call_count == 2


# ---------------------------------------------------------------------------
# Rate limit: ClientError with code 429 → LLMRateLimitError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_json_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 ClientError is surfaced as LLMRateLimitError."""
    rate_limit_exc = genai_errors.ClientError(
        429,
        {"error": {"code": 429, "message": "rate limit", "status": "RESOURCE_EXHAUSTED"}},
    )

    client_mock = _make_client_mock([rate_limit_exc])

    with (
        patch("nous.llm.client._build_client", return_value=client_mock),
        pytest.raises(LLMRateLimitError),
    ):
        await complete_json("some prompt", CompanyDescription)


# ---------------------------------------------------------------------------
# Missing API key → LLMError before any network call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_json_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """If GEMINI_API_KEY is empty, complete_json raises LLMError immediately."""
    monkeypatch.setenv("GEMINI_API_KEY", "")

    with pytest.raises(LLMError, match="GEMINI_API_KEY is not set"):
        await complete_json("some prompt", CompanyDescription)


# ---------------------------------------------------------------------------
# 5xx transient: ServerError twice then success → tenacity retries → returns result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_json_transient_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """ServerError (5xx) is retried by tenacity; eventual success returns the result."""
    server_exc = genai_errors.ServerError(
        503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}
    )

    # Two server errors then a successful response
    client_mock = _make_client_mock(
        [server_exc, server_exc, _make_response(VALID_JSON)]
    )

    with patch("nous.llm.client._build_client", return_value=client_mock):
        result = await complete_json("some prompt", CompanyDescription)

    assert isinstance(result, CompanyDescription)
    # tenacity retried within the first outer attempt; generate_content called 3 times
    assert client_mock.aio.models.generate_content.call_count == 3
