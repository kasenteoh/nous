"""Gemini wrapper. All LLM calls in nous go through complete_json().

Wraps google.genai with:
- JSON mode (response_mime_type="application/json")
- Pydantic schema enforcement
- One retry on Pydantic ValidationError (per CLAUDE.md)
- Tenacity backoff on rate-limit (429) and transient 5xx
- Custom exception hierarchy so callers can distinguish failure modes
"""

from __future__ import annotations

import logging
from typing import TypeVar

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

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gemini-2.5-flash"


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMParseError(LLMError):
    """Output didn't validate against the Pydantic schema after retry."""


class LLMRateLimitError(LLMError):
    """Provider returned a sustained 429."""


def _build_client() -> genai.Client:
    settings = Settings()
    if not settings.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY is not set; cannot call Gemini.")
    return genai.Client(api_key=settings.GEMINI_API_KEY)


async def complete_json(
    prompt: str,
    schema: type[T],
    *,
    model: str = DEFAULT_MODEL,
) -> T:
    """Send `prompt` to Gemini with JSON-mode + the schema, validate, return T.

    On ValidationError, retry exactly once with the same prompt.
    On HTTP rate-limit (429), raise LLMRateLimitError (no retry — caller can pause).
    On other transient errors (5xx), tenacity retries with exponential backoff up to 3 attempts.
    """
    client = _build_client()
    # google-genai accepts Pydantic model classes directly as response_schema,
    # which instructs the model to produce JSON matching the model's schema.
    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
    )

    async def _call() -> str:
        # Use the async client interface: client.aio.models.generate_content
        response = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return response.text or ""

    last_validation_error: ValidationError | None = None
    for attempt in (1, 2):
        try:
            # tenacity handles transient network/5xx; raises LLMRateLimitError on 429
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
            # ClientError covers all 4xx; code 429 is rate-limit specifically
            if exc.code == 429:
                raise LLMRateLimitError(str(exc)) from exc
            raise LLMError(str(exc)) from exc
        except genai_errors.ServerError as exc:
            # ServerError exhausted tenacity retries
            raise LLMError(str(exc)) from exc

        try:
            return schema.model_validate_json(raw_text)
        except ValidationError as exc:
            last_validation_error = exc
            logger.warning("LLM JSON did not validate (attempt %d): %s", attempt, exc)
            # loop continues to attempt 2 with same prompt

    raise LLMParseError(
        f"Output failed schema validation after retry: {last_validation_error}"
    ) from last_validation_error
