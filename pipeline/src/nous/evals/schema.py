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

Prompts whose runtime input is not a single document carry the extra inputs
in ``case.json`` (``roster``, ``claim``, ``profile`` + ``article_title``) or,
for ``company_match``, make ``input.txt`` the JSON candidate pair
(:class:`DedupCandidatePair`) — the "document" that prompt compares.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class CompanyProfile(BaseModel):
    """The tracked company's profile columns the entity guard renders into
    ``article_subject_match`` — named after the ``Company`` columns (NOT the
    prompt's labels) so the eval adapter maps them exactly as
    ``entity_guard.check_article_entity`` does."""

    website: str | None = None
    description_short: str | None = None
    industry_group: str | None = None
    hq_city: str | None = None
    hq_state: str | None = None


class DedupCandidate(BaseModel):
    """One side of a ``company_match`` candidate pair: the ``Company`` columns
    ``dedup_companies._CompanyRow`` projects. The eval adapter builds a real
    ``_CompanyRow`` from these and calls its ``to_prompt_dict`` — the exact
    rendering (description_long preferred, the latest-funding line) the dedup
    stage hands the prompt."""

    name: str
    website: str | None = None
    hq_city: str | None = None
    hq_state: str | None = None
    description_short: str | None = None
    description_long: str | None = None
    latest_round_type: str | None = None
    latest_round_amount: Decimal | None = None
    latest_round_date: date | None = None


class DedupCandidatePair(BaseModel):
    """``company_match`` ``input.txt``: the (A, B) pair the fuzzy pass judges."""

    a: DedupCandidate
    b: DedupCandidate


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
    claim: str = Field(
        default="",
        description=(
            "The single claim to check for a fact-verification prompt "
            "(source_verification), e.g. 'Acme raised $12M in its Series A round.' "
            "Empty for prompts that don't take a claim."
        ),
    )
    profile: CompanyProfile | None = Field(
        default=None,
        description=(
            "The tracked company's profile for article_subject_match (the "
            "company the article is about to attach to). None for prompts "
            "that don't take one."
        ),
    )
    article_title: str = Field(
        default="",
        description=(
            "The article headline for article_subject_match (input.txt is "
            "the stored body/snippet the guard passes as ``text``). Empty "
            "for prompts that don't take one."
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
