"""Provider-agnostic LLM wrapper. All LLM calls in nous go through complete_json().

Two backends, selected via Settings.LLM_PROVIDER:
- "gemini" (default): google.genai SDK, JSON mode via response_schema. Free
  tier is 20 RPD on gemini-2.5-flash (verified in-prod).
- "deepseek": OpenAI-compatible chat-completions API at api.deepseek.com.
  Paid (≈$0.27/1M input, $1.10/1M output as of 2026). Much higher rate
  limits than Gemini free tier. Spec rule "free tier first" is intentionally
  bypassed when this provider is chosen — flag it in code review.

Both backends honor:
- Pydantic schema validation on the response
- One retry on ValidationError (per CLAUDE.md)
- Tenacity exponential backoff on transient errors
- LLMRateLimitError on 429 (no retry; caller decides whether to pause)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Literal, TypeVar

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nous.config import Settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# Backwards-compat alias for any caller that still imports DEFAULT_MODEL.
DEFAULT_MODEL = DEFAULT_GEMINI_MODEL


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
    """Send `prompt` to the configured LLM provider, validate response against
    `schema`, return T.

    Provider is selected at call time from Settings().LLM_PROVIDER. If `model`
    is None, the provider's default model is used (gemini-2.5-flash for
    Gemini, deepseek-chat for DeepSeek).

    Common semantics across providers:
    - ValidationError → retry exactly once with the same prompt
    - 429 → LLMRateLimitError (no retry — caller decides)
    - 5xx / transient network → tenacity retries up to 3 attempts
    """
    settings = Settings()
    provider: Literal["gemini", "deepseek"] = settings.LLM_PROVIDER
    if provider == "deepseek":
        return await _complete_json_deepseek(
            prompt, schema, model=model or DEFAULT_DEEPSEEK_MODEL, settings=settings
        )
    return await _complete_json_gemini(
        prompt, schema, model=model or DEFAULT_GEMINI_MODEL, settings=settings
    )


# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------


def _build_gemini_client(settings: Settings) -> genai.Client:
    if not settings.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY is not set; cannot call Gemini.")
    return genai.Client(api_key=settings.GEMINI_API_KEY)


async def _complete_json_gemini(
    prompt: str,
    schema: type[T],
    *,
    model: str,
    settings: Settings,
) -> T:
    client = _build_gemini_client(settings)
    # google-genai accepts Pydantic model classes directly as response_schema,
    # which instructs the model to produce JSON matching the model's schema.
    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
    )

    async def _call() -> str:
        response = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return response.text or ""

    last_validation_error: ValidationError | None = None
    for attempt in (1, 2):
        try:
            async for retry_attempt in AsyncRetrying(
                wait=wait_exponential(multiplier=1, min=1, max=10),
                stop=stop_after_attempt(3),
                retry=retry_if_exception_type(genai_errors.ServerError),
                reraise=True,
            ):
                with retry_attempt:
                    raw_text = await _call()
                    break
        except genai_errors.ClientError as exc:
            if exc.code == 429:
                raise LLMRateLimitError(str(exc)) from exc
            raise LLMError(str(exc)) from exc
        except genai_errors.ServerError as exc:
            raise LLMError(str(exc)) from exc

        try:
            return schema.model_validate_json(raw_text)
        except ValidationError as exc:
            last_validation_error = exc
            logger.warning(
                "Gemini JSON did not validate (attempt %d): %s", attempt, exc
            )

    raise LLMParseError(
        f"Output failed schema validation after retry: {last_validation_error}"
    ) from last_validation_error


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

    # DeepSeek doesn't accept Pydantic schemas as a parameter the way Gemini
    # does, so we include the JSON Schema in the system prompt as a hint and
    # rely on response_format={"type": "json_object"} + post-hoc Pydantic
    # validation. This pattern is documented in DeepSeek's API guide.
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
