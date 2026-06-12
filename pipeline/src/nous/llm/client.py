"""DeepSeek LLM wrapper. All LLM calls in nous go through complete_json().

Backend: DeepSeek's OpenAI-compatible chat-completions API at
api.deepseek.com. Paid (≈$0.27/1M input, $1.10/1M output as of 2026). The
spec rule "free tier first" is intentionally bypassed — a deliberate,
cost-incurring choice, made because Gemini's free tier (20 RPD on
gemini-2.5-flash) was too low for bulk enrichment.

The wrapper honors:
- Pydantic schema validation on the response
- One retry on ValidationError (per CLAUDE.md)
- Tenacity exponential backoff on transient (5xx) errors
- LLMRateLimitError on 429 (no retry; caller decides whether to pause)
"""

from __future__ import annotations

import json
import logging
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nous.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# Shared ceiling for prompt input text across all LLM-using stages.
# Stages that deliberately use a smaller limit (e.g. TechCrunch headline
# articles ~6k) keep their own local constant and note it is intentionally
# below this shared ceiling.
MAX_PROMPT_INPUT_CHARS: int = 32_000


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMParseError(LLMError):
    """Output didn't validate against the Pydantic schema after retry."""


class LLMRateLimitError(LLMError):
    """Provider returned a sustained 429."""


async def complete_json(
    prompt: str,
    schema: type[T],
    *,
    model: str | None = None,
) -> T:
    """Send `prompt` to DeepSeek, validate the response against `schema`, return T.

    If `model` is None, the default model (deepseek-chat) is used.

    Semantics:
    - ValidationError → retry exactly once with the same prompt
    - 429 → LLMRateLimitError (no retry — caller decides)
    - 5xx / transient network → tenacity retries up to 3 attempts
    """
    settings = Settings()
    return await _complete_json_deepseek(
        prompt, schema, model=model or DEFAULT_DEEPSEEK_MODEL, settings=settings
    )


# ---------------------------------------------------------------------------
# DeepSeek backend (OpenAI-compatible chat completions API)
# ---------------------------------------------------------------------------


async def _complete_json_deepseek(
    prompt: str,
    schema: type[T],
    *,
    model: str,
    settings: Settings,
) -> T:
    if not settings.DEEPSEEK_API_KEY:
        raise LLMError("DEEPSEEK_API_KEY is not set; cannot call DeepSeek.")

    # DeepSeek doesn't accept a Pydantic/JSON schema as a request parameter, so
    # we include the JSON Schema in the system prompt as a hint and rely on
    # response_format={"type": "json_object"} + post-hoc Pydantic validation.
    # This pattern is documented in DeepSeek's API guide.
    schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
    system_message = (
        "You output exactly one JSON object that conforms to the schema below.\n"
        "Do not include any prose, markdown fences, comments, or extra fields.\n"
        "Use null (not the string 'null') for fields you can't determine.\n"
        f"Schema: {schema_json}"
    )

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    async def _call() -> str:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                json=body,
                headers=headers,
            )
        if resp.status_code == 429:
            raise LLMRateLimitError(f"DeepSeek 429: {resp.text}")
        if resp.status_code >= 500:
            # Synthesize a status error tenacity can retry on.
            raise httpx.HTTPStatusError(
                f"DeepSeek {resp.status_code}: {resp.text}",
                request=resp.request,
                response=resp,
            )
        if resp.status_code >= 400:
            # Other 4xx (auth, bad request) — non-retryable, surface as LLMError.
            raise LLMError(
                f"DeepSeek {resp.status_code}: {resp.text}"
            )
        data = resp.json()
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"DeepSeek response missing choices[0].message.content: {data!r}"
            ) from exc

    last_validation_error: ValidationError | None = None
    for attempt in (1, 2):
        try:
            async for retry_attempt in AsyncRetrying(
                wait=wait_exponential(multiplier=1, min=1, max=10),
                stop=stop_after_attempt(3),
                retry=retry_if_exception_type(httpx.HTTPStatusError),
                reraise=True,
            ):
                with retry_attempt:
                    raw_text = await _call()
                    break
        except httpx.HTTPStatusError as exc:
            # 5xx tenacity exhausted
            raise LLMError(str(exc)) from exc

        try:
            return schema.model_validate_json(raw_text)
        except ValidationError as exc:
            last_validation_error = exc
            logger.warning(
                "DeepSeek JSON did not validate (attempt %d): %s", attempt, exc
            )

    raise LLMParseError(
        f"Output failed schema validation after retry: {last_validation_error}"
    ) from last_validation_error
