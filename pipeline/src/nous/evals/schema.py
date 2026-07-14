"""Pydantic v2 schemas for golden-set fixtures and eval reports.

Fixture layout (one directory per case under
``tests/golden/<prompt>/cases/<case_id>/``):

- ``input.txt``     — the document text the prompt receives (cleaned page
  text / article body, i.e. what the runtime stage passes to
  ``build_prompt`` after ``extract_visible_text`` + truncation).
- ``case.json``     — :class:`CaseSpec`: prompt inputs beyond the document
  (company name, prompt variant) plus reviewer notes.
- ``expected.json`` — hand-checked ground-truth extraction. Must validate
  against the prompt's response schema.
- ``recorded.json`` — :class:`RecordedResponse`: a recorded model response.
  ``provenance`` says where it came from ("simulated" for hand-authored
  stand-ins, "deepseek" once record mode has refreshed it live).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CaseSpec(BaseModel):
    """Per-case prompt inputs and provenance notes (``case.json``)."""

    company_name: str
    variant: str = Field(
        default="default",
        description=(
            "Which prompt template to use for prompts that have more than "
            "one (e.g. funding_extraction: 'news' vs 'website')."
        ),
    )
    roster: list[tuple[str, str]] = Field(
        default_factory=list,
        description=(
            "(name, role) leadership roster for prompts that take one as an "
            "allow-list input (career_history). Empty for prompts that don't."
        ),
    )
    notes: str = Field(
        default="",
        description="Reviewer notes: what this case exercises and why.",
    )


class RecordedResponse(BaseModel):
    """A recorded model response for one case (``recorded.json``)."""

    provenance: Literal["simulated", "deepseek"] = Field(
        description=(
            "'simulated' for hand-authored stand-in responses (no API key "
            "was available when the fixture was created); 'deepseek' once "
            "record mode has replaced it with a live model response."
        ),
    )
    model: str | None = Field(
        default=None,
        description="Model id that produced the response (record mode).",
    )
    recorded_at: str | None = Field(
        default=None,
        description="ISO-8601 timestamp of the live recording (record mode).",
    )
    response: dict[str, Any] = Field(
        description=(
            "The JSON object the model returned. Replayed through the "
            "runtime schema-validation path when scoring offline."
        ),
    )


class PromptReport(BaseModel):
    """Aggregate metrics for one prompt's golden set."""

    prompt: str
    case_count: int
    provenance_counts: dict[str, int] = Field(
        default_factory=dict,
        description="How many recordings are simulated vs live-deepseek.",
    )
    metrics: dict[str, float] = Field(
        description="Metric name -> value in [0, 1] (insertion-ordered).",
    )
    gated: list[str] = Field(
        description="Names of metrics gated against baseline floors.",
    )
    issues: dict[str, list[str]] = Field(
        default_factory=dict,
        description="case_id -> human-readable mismatch notes.",
    )
