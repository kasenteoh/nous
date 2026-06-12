"""Funding-extraction prompt per spec §6.2 (M3).

Input: cleaned text of a news article that may or may not be a funding
announcement, and the name of the company we're asking about. Output: a
Pydantic model capturing the structured round data, or a flagged non-match.

Per CLAUDE.md ("prompts must instruct the model to return null or empty
rather than fabricate"), the template explicitly tells the model to leave
fields null when a value isn't stated in the article.

This module is a drop-in user of `nous.llm.client.complete_json`. The caller
(Chunk 6a — extract-funding stage) imports `build_prompt` and
`FundingExtraction` and hands them to `complete_json`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from nous.llm.client import MAX_PROMPT_INPUT_CHARS

# News articles are usually well under the shared ceiling, but we truncate
# defensively so a malformed scrape can't blow the prompt budget.
# Uses the shared MAX_PROMPT_INPUT_CHARS ceiling (32_000).
MAX_ARTICLE_CHARS = MAX_PROMPT_INPUT_CHARS


class FundingExtraction(BaseModel):
    is_funding_announcement: bool = Field(
        ...,
        description=(
            "True only if the article is primarily announcing a funding round "
            "for the named company. False for tangential mentions, profiles, "
            "or rounds for a different company."
        ),
    )
    round_type: str | None = Field(
        default=None,
        description=(
            "Round label as stated in the article: 'Pre-Seed', 'Seed', "
            "'Series A', 'Series B', etc. Null if not explicitly named."
        ),
    )
    amount_raised_usd: Decimal | None = Field(
        default=None,
        description=(
            "Round size in raw USD (e.g. 50000000 for '$50M'). Null if the "
            "article does not state a number."
        ),
    )
    valuation_post_money_usd: Decimal | None = Field(
        default=None,
        description=(
            "Post-money valuation in raw USD. Null if the article does not "
            "state a valuation."
        ),
    )
    valuation_source: str | None = Field(
        default=None,
        description=(
            "Short attribution for the post-money valuation: the publication "
            "or source name plus month/year if stated, e.g. 'TechCrunch, "
            "March 2026'. Null when no publication/source is named alongside "
            "the valuation. Do NOT invent a source."
        ),
    )
    announced_date: date | None = Field(
        default=None,
        description=(
            "The date the round was publicly announced. Null if unclear."
        ),
    )
    lead_investors: list[str] = Field(
        default_factory=list,
        description=(
            "Firms the article identifies as leading the round. Empty list "
            "if no lead is named."
        ),
    )
    other_investors: list[str] = Field(
        default_factory=list,
        description=(
            "Other participating investors named in the article."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        ...,
        description=(
            "'high' when amount + lead are both stated explicitly; 'medium' "
            "when one is inferred; 'low' when the article is fuzzy or the "
            "data is only implied."
        ),
    )


PROMPT_TEMPLATE = """\
You are extracting funding-round data from a news article.

Company name being asked about: {company_name}

Return JSON matching the schema. Rules:
- If the article is NOT primarily a funding announcement for {company_name},
  set is_funding_announcement=false and leave other fields null/empty.
- Do not invent numbers. If the round size or valuation is not stated, return null.
- amount_raised_usd is in raw USD (e.g. 50000000 for "$50M").
- valuation_source: if a publication or attribution accompanies the valuation
  number (e.g. "according to TechCrunch", "sources told The Information"),
  capture it as a short string like "TechCrunch, March 2026". Return null if
  no source is named alongside the valuation — never invent.
- announced_date is the date the round was publicly announced; null if unclear.
- lead_investors: only firms the article identifies as leading. Other participants
  go in other_investors.
- confidence: 'high' if amount + lead are both stated explicitly; 'medium' if
  one is inferred; 'low' if the article is fuzzy or the data is implied.

Article body:
---
{article_text}
---
"""


def build_prompt(*, company_name: str, article_text: str) -> str:
    """Render the funding-extraction prompt with the given inputs.

    `article_text` is truncated to MAX_ARTICLE_CHARS (= MAX_PROMPT_INPUT_CHARS)
    to bound prompt cost.
    """
    return PROMPT_TEMPLATE.format(
        company_name=company_name,
        article_text=article_text[:MAX_ARTICLE_CHARS],
    )


WEBSITE_PROMPT_TEMPLATE = """\
You are extracting funding-round data from a company's OWN public website
(homepage + about/press pages), not a news article. This is a fallback source,
so be conservative.

Company name: {company_name}

Return JSON matching the schema. Rules:
- Only report a round the site EXPLICITLY states (e.g. a press/news blurb like
  "we raised our $20M Series B"). If the site does not clearly state funding,
  set is_funding_announcement=false and leave the other fields null/empty.
- Do not invent numbers. If the round size or valuation is not stated, return null.
- amount_raised_usd is in raw USD (e.g. 50000000 for "$50M").
- If MULTIPLE funding events or dates appear on the page, report only the MOST
  RECENT one — use the latest date you can find as announced_date.
- valuation_source: set to "Company website" followed by the latest relevant
  date if one is shown (e.g. "Company website, March 2026"). Never invent a
  third-party publication.
- confidence: at most 'medium' for website-sourced data; 'low' when the figure
  is only implied. A company's own site is less authoritative than news coverage.

Website text (may be truncated):
---
{page_text}
---
"""


def build_website_prompt(*, company_name: str, page_text: str) -> str:
    """Render the website-fallback funding prompt.

    Reuses the FundingExtraction schema but tells the model this is the
    company's own site (lower authority) and to prefer the latest date on the
    page. `page_text` is truncated to MAX_ARTICLE_CHARS to bound prompt cost.
    """
    return WEBSITE_PROMPT_TEMPLATE.format(
        company_name=company_name,
        page_text=page_text[:MAX_ARTICLE_CHARS],
    )
