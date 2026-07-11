"""Eligibility-judgment prompt for the judge-eligibility backfill stage.

Input: a company's stored description + scraped site text. Output: the same
is_startup / hq_country / founded_year judgment the enrichment prompt makes,
WITHOUT re-writing descriptions (enrichment is write-once). Used to backfill
companies enriched before the judgment existed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Version stamped onto rows whose content this prompt produced (companies
# eligibility fields via judge-eligibility). Scheme: "<date>.<same-day-counter>".
# Bump on ANY semantic change to the template, schema, or validators — even a
# wording tweak — so data from a bad revision can be found and re-run.
PROMPT_VERSION: str = "2026-07-10.1"


class EligibilityJudgment(BaseModel):
    is_startup: bool | None = Field(
        default=None,
        description=(
            "True when this reads like a venture-scale SOFTWARE startup: an "
            "independent, private company, founded within roughly the last 15 "
            "years, whose core offering is a software product or platform. "
            "False when it clearly is NOT one — e.g. a business/web directory "
            "or listings site, a coaching / courses / 'mindset' / info-product "
            "business, a marketing or dev agency / consultancy, a long-"
            "established (15+ years) non-venture business, a local/lifestyle "
            "SMB, a decades-old enterprise, a publicly traded company, a "
            "subsidiary, a fund, or a media property. Null when the text does "
            "not support a confident call — never guess, and when genuinely "
            "unsure prefer null (do NOT exclude a possible real startup)."
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
            "Headquarters country as a 2-letter ISO code (e.g. 'US', 'IN', 'GB'). "
            "Set this when the text CLEARLY implies a country — a formal HQ line, "
            "an address, 'UK-based', 'headquartered in Bangalore', a non-US city "
            "name, or similar. You do NOT need an explicit 'HQ:' line; a clear "
            "implication is enough. Null when the text is ambiguous or silent on "
            "location — never invent a country you don't see."
        ),
    )


PROMPT_TEMPLATE = """\
You are curating a discovery catalog of US software startups. Decide whether
the company below belongs, based ONLY on the text provided.

Rules:
- `is_startup`: true ONLY for a venture-scale SOFTWARE startup — an
  independent, PRIVATE company, founded within roughly the last 15 years,
  whose core offering is a software product or platform (SaaS, a developer
  tool, an app, an API, AI/data infrastructure, a marketplace platform, etc.).
  Set it FALSE when the text clearly describes one of these instead:
    • a business/web directory, listings site, or yellow-pages-style index
      that aggregates other businesses (e.g. "online directory connecting
      consumers with local businesses");
    • a coaching, courses, training, "mindset", masterclass, or other
      info-product / personal-brand business (selling knowledge, programs,
      or content rather than software);
    • a marketing, advertising, design, dev, or other agency / consultancy
      that sells services or staff-for-hire rather than a product;
    • a long-established business — roughly 15+ years old, or one that
      advertises decades of operation — that is plainly not venture-backed;
    • a lifestyle business or local small business (a single shop,
      restaurant, salon, clinic, brokerage, local service provider, etc.);
    • a decades-old enterprise, a publicly traded company, a subsidiary or
      division of a larger company, an investment fund, or a media/news
      property.
  When the text does not support a confident call, return null. Never guess —
  and when you are genuinely unsure whether a borderline company qualifies,
  prefer null (or true) over false: excluding a real startup hides it from the
  catalog, which is worse than briefly keeping a borderline one.
- `not_startup_reason`: one short factual sentence, only when is_startup is
  false (e.g. "Online business directory, not a software product" or
  "Coaching / courses business, not a software startup").
- `founded_year`: only when the text states it. Null otherwise — never fabricate.
- `hq_country`: a 2-letter ISO code (e.g. 'US', 'IN', 'GB'). Set this when
  the text CLEARLY implies a country — a formal HQ line, an address, 'UK-based',
  'headquartered in Bangalore', a recognisable non-US city, or similar. You do
  NOT need an explicit "HQ:" field; a clear implication is sufficient. Null when
  the text is ambiguous or silent on country — never invent a value you don't see.

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
