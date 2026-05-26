"""Company-description prompt per spec §6.1.

Input: cleaned visible text from a company's homepage + about/product
subpages.  Output: a Pydantic model with short + long descriptions,
primary category, and tags.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CompanyDescription(BaseModel):
    description_short: str = Field(
        ...,
        description="1–2 sentences. Plain language. No marketing fluff.",
    )
    description_long: str = Field(
        ...,
        description=(
            "3–6 paragraphs of markdown. What the product does, who it's for, "
            "how it works, what makes it distinctive. Write like a curious "
            "analyst, not a press release."
        ),
    )
    primary_category: str = Field(
        ...,
        description="e.g. 'developer tools', 'fintech', 'AI infrastructure'.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Lowercase, hyphenated tags (max ~8).",
    )


PROMPT_TEMPLATE = """\
You are an analyst writing a short profile of the company below. You will read
text scraped from their public website (homepage + about/product/team pages)
and produce a JSON object that matches the provided schema.

Rules:
- Strip marketing language. Write like a curious analyst, not a press release.
- If the page is thin or unclear about what the company does, say so plainly in
  the description (e.g. "The website does not clearly describe the product").
  Do NOT invent details that aren't supported by the text.
- The long description should be 3–6 paragraphs of markdown a reader would
  actually enjoy: what they build, who it's for, how it works, what's
  distinctive about it.
- `primary_category` should be a common bucket like "developer tools",
  "fintech", "AI infrastructure", "vertical SaaS", "consumer", "biotech tooling".
  Don't invent obscure categories.
- `tags`: up to 8 lowercase, hyphenated technical/category tags.

Company name: {company_name}

Website text (may be truncated):
---
{cleaned_text}
---

Return JSON only.
"""


def build_prompt(*, company_name: str, cleaned_text: str) -> str:
    return PROMPT_TEMPLATE.format(
        company_name=company_name,
        cleaned_text=cleaned_text,
    )
