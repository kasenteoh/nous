"""Eligibility-judgment prompt for the judge-eligibility backfill stage.

Input: a company's stored description + scraped site text. Output: the same
is_startup / hq_country / founded_year judgment the enrichment prompt makes,
WITHOUT re-writing descriptions (enrichment is write-once). Used to backfill
companies enriched before the judgment existed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EligibilityJudgment(BaseModel):
    is_startup: bool | None = Field(
        default=None,
        description=(
            "True when this reads like an operating startup: an independent, "
            "private company founded within roughly the last 15 years. False "
            "when it clearly is not (decades-old enterprise, publicly traded, "
            "a subsidiary, a fund, a media site). Null when the text does not "
            "support a confident call — never guess."
        ),
    )
    not_startup_reason: str | None = Field(
        default=None,
        description="One short factual sentence; only when is_startup is false.",
    )
    founded_year: int | None = Field(
        default=None,
        description="Founding year ONLY if the text states it. Null otherwise.",
    )
    hq_country: str | None = Field(
        default=None,
        description=(
            "Headquarters country as a 2-letter ISO code ONLY when the text "
            "clearly states it. Null otherwise — never guess."
        ),
    )


PROMPT_TEMPLATE = """\
You are curating a discovery catalog of US software startups. Decide whether
the company below belongs, based ONLY on the text provided.

Rules:
- `is_startup`: true only for an independent, PRIVATE company founded within
  roughly the last 15 years. False for decades-old enterprises, publicly
  traded companies, subsidiaries, funds, or media properties. If the text
  does not support a confident call, return null. Never guess.
- `not_startup_reason`: one short factual sentence, only when is_startup is
  false (e.g. "Founded in 2000; publicly traded enterprise").
- `founded_year` / `hq_country`: only when the text states them. `hq_country`
  is a 2-letter ISO code (e.g. 'US', 'IN', 'GB'). Null otherwise — never
  fabricate.

Company name: {company_name}

Stored description:
---
{description}
---

Website text (may be truncated):
---
{cleaned_text}
---

Return JSON only.
"""


def build_prompt(
    *, company_name: str, description: str, cleaned_text: str
) -> str:
    return PROMPT_TEMPLATE.format(
        company_name=company_name,
        description=description,
        cleaned_text=cleaned_text,
    )
