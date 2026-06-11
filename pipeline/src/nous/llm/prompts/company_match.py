"""Company-match adjudication prompt (dedup stage).

Input: two candidate company records — each with name / website / short
description / HQ city,state — surfaced as a possible duplicate by fuzzy
signals (similar name, shared HQ). Output: a Pydantic model stating whether
they are the SAME real-world company, with a confidence band.

The dedup stage only merges on ``same_company=true`` AND ``confidence='high'``,
so the prompt is tuned to be conservative: it must default to *not* a match
when the evidence is ambiguous.

Per CLAUDE.md ("prompts must instruct the model to return null or empty rather
than fabricate"), the template tells the model to return same_company=false /
low confidence when it is unsure rather than guess.

This module is a drop-in user of ``nous.llm.client.complete_json``. The caller
(the dedup-companies stage) imports ``build_company_match_prompt`` and
``CompanyMatch`` and hands them to ``complete_json``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CompanyMatch(BaseModel):
    same_company: bool = Field(
        ...,
        description=(
            "True ONLY when the two records describe the same real-world "
            "company. False for distinct companies, even when names or "
            "categories are similar."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        ...,
        description=(
            "'high' when multiple independent signals agree (e.g. matching "
            "domain or an unmistakable name + corroborating description/HQ); "
            "'medium' when the match is likely but rests on a single signal; "
            "'low' when the evidence is weak or conflicting. When unsure, "
            "return same_company=false with low confidence."
        ),
    )


# Sentinel shown for any field we don't have, so the model isn't misled into
# treating a blank line as a meaningful empty string.
_UNKNOWN = "(unknown)"


def _fmt(value: str | None) -> str:
    value = (value or "").strip()
    return value if value else _UNKNOWN


def _render_company(label: str, company: dict[str, object]) -> str:
    name = _fmt(_as_str(company.get("name")))
    website = _fmt(_as_str(company.get("website")))
    description = _fmt(_as_str(company.get("description")))
    city = _as_str(company.get("hq_city"))
    state = _as_str(company.get("hq_state"))
    location_parts = [p for p in (city, state) if p and p.strip()]
    location = ", ".join(location_parts) if location_parts else _UNKNOWN
    return (
        f"Company {label}:\n"
        f"- Name: {name}\n"
        f"- Website: {website}\n"
        f"- Description: {description}\n"
        f"- HQ: {location}"
    )


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


PROMPT_TEMPLATE = """\
You are deciding whether two records refer to the SAME real-world software
company, for the purpose of merging duplicate database rows.

{company_a}

{company_b}

Weigh the evidence holistically:
- Domain: the same registrable website domain is strong evidence of a match;
  clearly different company domains are strong evidence against.
- Name: account for casing, punctuation, legal suffixes (Inc., LLC), and
  abbreviations. Similar-but-distinct names (e.g. a common word both companies
  use) are NOT by themselves a match.
- Description: do the two descriptions point to the same product/business?
- Location: a shared HQ city/state corroborates a match; a clear conflict
  weakens it (but remote-first companies move, so treat location as supporting
  evidence, not decisive on its own).

Rules:
- Set same_company=true ONLY when you are genuinely confident the two records
  are the same entity. Two different companies in the same space is the common
  case — do not merge them.
- When the evidence is ambiguous, conflicting, or thin, return
  same_company=false with low confidence. Never guess.
- Reserve confidence='high' for matches backed by more than one agreeing
  signal (e.g. domain match, or an unmistakable name plus a corroborating
  description or HQ).

Return JSON matching the schema.
"""


def build_company_match_prompt(a: dict[str, object], b: dict[str, object]) -> str:
    """Render the company-match prompt for candidate pair ``(a, b)``.

    Each dict is expected to carry the keys ``name``, ``website``,
    ``description`` (a short description), ``hq_city``, and ``hq_state``.
    Missing or empty values are rendered as ``(unknown)`` so the model isn't
    misled by blank fields. Extra keys are ignored.
    """
    return PROMPT_TEMPLATE.format(
        company_a=_render_company("A", a),
        company_b=_render_company("B", b),
    )
