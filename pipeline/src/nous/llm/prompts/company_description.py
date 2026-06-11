"""Company-description prompt per spec §6.1.

Input: cleaned visible text from a company's homepage + about/product
subpages.  Output: a Pydantic model with short + long descriptions,
primary category, and tags.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PersonExtraction(BaseModel):
    name: str = Field(..., description="Full name of the person.")
    role: str = Field(
        ...,
        description=(
            "Their role/title at the company, e.g. 'CEO', 'CTO', 'Founder', "
            "'Co-founder & CEO'. Use the title as stated on the site."
        ),
    )


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
    people: list[PersonExtraction] = Field(
        default_factory=list,
        description=(
            "Founders and senior leadership (CEO, CTO, and other C-level/"
            "founder roles) named on the site. Empty list if none are stated."
        ),
    )
    hq_city: str | None = Field(
        default=None,
        description=(
            "Headquarters city, when clearly stated in the text. "
            "Null if not stated — never guess."
        ),
    )
    hq_state: str | None = Field(
        default=None,
        description=(
            "Headquarters US state as a 2-letter code (e.g. 'CA', 'NY') when "
            "determinable. Null if not stated or not US — never guess."
        ),
    )
    industry: str | None = Field(
        default=None,
        description=(
            "Coarse industry bucket (e.g. 'fintech', 'developer tools', "
            "'AI infrastructure', 'healthcare'). Null if unclear — never guess."
        ),
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
- `people`: list the founders and senior leadership (CEO, CTO, and other
  C-level or founder roles) that the site actually names — typically from an
  about/team/leadership page. Use the role exactly as stated. Return an EMPTY
  list if the site does not clearly name them. Do NOT guess or fabricate names.
- `hq_city` / `hq_state`: the company's headquarters location, US-focused.
  Extract these ONLY when the text clearly states them (an address, a
  "headquartered in ..." line, or a contact/footer location). `hq_state` must
  be a 2-letter US state code; leave it null if the HQ is outside the US or the
  state is not given. Return null — do NOT guess a location the text doesn't state.
- `industry`: a coarse industry bucket like "fintech", "developer tools",
  "AI infrastructure", or "healthcare". Return null if the text does not make
  the industry clear. Never fabricate one.

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
