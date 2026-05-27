"""Headline company-extraction prompt (M3, TechCrunch discovery).

Input: a news headline + RSS snippet from a startup-funding news feed (the
TechCrunch venture tag). Output: whether the item announces a company raising
a round and, if so, the funded company's name — extracted by the model rather
than a brittle "<Name> <verb>" title regex, which mis-parsed headlines like
"How Lucra raised $20M" and "Amazon fulfillment competitor Stord raises $250M".

Per CLAUDE.md ("prompts must instruct the model to return null or empty rather
than fabricate"), the model returns company_name=null when the item isn't a
funding announcement or the company can't be identified.

Drop-in user of nous.llm.client.complete_json.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# RSS snippets are short; truncate defensively so a malformed feed entry can't
# blow the prompt budget.
MAX_SNIPPET_CHARS = 2_000


class HeadlineCompany(BaseModel):
    is_funding_announcement: bool = Field(
        ...,
        description=(
            "True only if the item is primarily about a company raising a "
            "funding round (seed, Series A/B/C, growth, etc.)."
        ),
    )
    company_name: str | None = Field(
        default=None,
        description=(
            "The name of the company that RAISED the funding, with no "
            "descriptive lead-in. Null when is_funding_announcement is false "
            "or the company cannot be identified."
        ),
    )


PROMPT_TEMPLATE = """\
You are reading a news item from a startup-funding news feed. Decide whether it
announces a specific company raising a funding round, and if so, name that
company.

Rules:
- company_name is the startup that RAISED money — never the investor or VC firm,
  and never a larger company mentioned only for context or comparison.
- Return the clean company name only, stripping any descriptive lead-in or
  framing. Examples:
    "Marketing operating system Nectar Social raises $30M" -> "Nectar Social"
    "How Lucra raised $20M as an eSports play"             -> "Lucra"
    "Amazon fulfillment competitor Stord raises $250M"     -> "Stord"
- If the item is not primarily a funding announcement for a specific company,
  set is_funding_announcement=false and company_name=null.
- Do not invent a name. If you cannot identify the company, return null.

Headline:
{title}

Snippet:
{snippet}
"""


def build_prompt(*, title: str, snippet: str) -> str:
    """Render the headline company-extraction prompt."""
    cleaned = (snippet or "").strip()[:MAX_SNIPPET_CHARS]
    return PROMPT_TEMPLATE.format(title=title.strip(), snippet=cleaned or "(none)")
