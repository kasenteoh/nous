"""Company-description prompt per spec §6.1.

Input: cleaned visible text from a company's homepage + about/product
subpages.  Output: a Pydantic model with short + long descriptions,
primary category, and tags.
"""

from __future__ import annotations

from typing import Literal

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
    website_state: Literal[
        "ok",
        "parked_or_for_sale",
        "under_construction",
        "unrelated_site",
        "insufficient_info",
    ] = Field(
        ...,
        description=(
            "'ok' when the text reads like the company's own operating site. "
            "'parked_or_for_sale' for domain-sale/parking/registrar pages. "
            "'under_construction' for launching-soon/placeholder pages. "
            "'unrelated_site' when the text is about a DIFFERENT business "
            "than the named company. 'insufficient_info' when there is too "
            "little text to tell."
        ),
    )
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
        description="One short sentence; only when is_startup is false.",
    )
    founded_year: int | None = Field(
        default=None,
        description="Founding year ONLY if the text states it. Null otherwise.",
    )
    hq_country: str | None = Field(
        default=None,
        description=(
            "Headquarters country as a 2-letter ISO code (e.g. 'US', 'IN', "
            "'GB') ONLY when the text clearly states it. Null otherwise — "
            "never guess."
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
- `website_state`: classify the page itself. Use 'parked_or_for_sale' for
  domain-sale/parking/registrar placeholder pages, 'under_construction' for
  launching-soon pages with no product info, 'unrelated_site' when the text
  describes a different business than {company_name}, 'insufficient_info'
  when there is too little text to tell, and 'ok' otherwise. When the state
  is not 'ok', still fill the description fields with a one-line factual note
  (they will not be published).
- `is_startup`: true only for an independent, PRIVATE company founded within
  roughly the last 15 years. False for decades-old enterprises, publicly
  traded companies, subsidiaries, funds, or media properties. If the text
  does not support a confident call, return null. Never guess.
- `not_startup_reason`: one short factual sentence, only when is_startup is
  false (e.g. "Founded in 2000; publicly traded enterprise").
- `founded_year` / `hq_country`: only when the text states them. `hq_country`
  is a 2-letter ISO code. Null otherwise — never fabricate.

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
