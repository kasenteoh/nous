"""Dedicated long-form company-description prompt (W-F).

The original ``company_description`` prompt did six jobs in one call, and the
classification instructions crowded the description ask down to ~2 lines —
DeepSeek wrote three short paragraphs and stopped. This prompt exists to do
exactly one job: the long-form profile a reader would actually enjoy.

Split of duties with :mod:`nous.llm.prompts.company_description` (the judge):

- **judge** — website_state, is_startup, people, HQ, taxonomy, and the 1–2
  sentence ``description_short``. Runs first, on every enrichable company.
- **this prompt** — ``description_long`` only. Runs second, and only when the
  judge kept the company in the catalog (website ok, not excluded) and the
  scraped text is substantive enough to support a real profile
  (see ``_MIN_DESCRIBE_CHARS`` in :mod:`nous.pipeline.enrich_companies`).

Input: cleaned visible text from the company's homepage + subpages. Output:
a single nullable markdown description — null over fabrication, per the
repo-wide LLM rule.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# Version stamped onto companies.enrichment_prompt_version for rows whose
# description_long state this prompt produced (including a deliberate
# "too thin to describe" skip — see enrich_companies). Scheme:
# "<date>.<same-day-counter>". Bump on ANY semantic change to the template,
# schema, or validators so outdated descriptions can be found and re-run via
# ``enrich-companies --redescribe-outdated``.
#
# Starts at "2026-07-11.1", NOT "2026-07-10.1": the pre-split single prompt
# (company_description at 2026-07-10.1, migration-0031 era) stamped the same
# column, so this prompt's version must sort strictly ABOVE that cohort or
# --redescribe-outdated's `< current` selection would silently skip every row
# the old prompt enriched (caught by test_redescribe_selection_boundaries in
# CI). The handful of rows the new prompt stamped 2026-07-10.1 before this
# bump simply get re-described once more (idempotent, ~cents).
PROMPT_VERSION: str = "2026-07-11.1"

# Input ceiling for THIS call. Deliberately above the shared 32k
# MAX_PROMPT_INPUT_CHARS ceiling in nous.llm.client: the whole point of the
# dedicated description pass is richer source material, and the judge call
# (which runs on every enrichable company) keeps the shared ceiling so the
# extra input cost is paid only by companies that earn a profile. 48k chars
# ≈ 12k tokens ≈ $0.0032 of input per call at current DeepSeek pricing.
MAX_DESCRIPTION_INPUT_CHARS: int = 48_000


class CompanyLongDescription(BaseModel):
    """Response schema: the long-form profile, or null when unsupportable."""

    description_long: str | None = Field(
        default=None,
        description=(
            "Markdown paragraphs. ~350-600 words across 4-7 paragraphs when "
            "the source text is rich; fewer, shorter paragraphs when it is "
            "thin. Null when the text cannot support an honest profile."
        ),
    )

    @field_validator("description_long")
    @classmethod
    def _blank_is_null(cls, value: str | None) -> str | None:
        """An empty / whitespace-only description is the same as null."""
        if value is not None and not value.strip():
            return None
        return value


PROMPT_TEMPLATE = """\
You are an analyst writing the long-form profile of {company_name} for a
startup-discovery site. The company has already been vetted separately — do
NOT classify or judge it. Your entire job is the profile text. You will read
text scraped from the company's public website (homepage + about/product/team
pages) and return a JSON object matching the provided schema.

Grounding rules — these outrank everything below:
- Every claim must be supported by the website text. If the text is thin or
  vague on some dimension, either skip that dimension or say so plainly
  (e.g. "The site does not explain how the product works"). Write only what
  IS supportable. Never pad, never invent; unknown stays unknown.
- Notable customers, partners, metrics, and traction may be mentioned ONLY
  when the text states them. Never estimate, extrapolate, or round numbers
  the text does not contain.
- If the text is too thin to support even a short honest profile, return
  {{"description_long": null}} — null is always better than filler.

What to cover — each dimension ONLY when the source text supports it:
- The problem: what pain or gap the company addresses, and for whom.
- The product: what it actually does and how it works, including the
  technical approach when the site states one.
- Users and use cases: who buys or uses it, with the concrete use cases the
  site names.
- Business model: how the company makes money (pricing tiers, usage-based
  billing, marketplace take, open-core, enterprise contracts, ...) when
  stated.
- Market context: the alternatives, incumbent tools, or manual workflow it
  replaces, and how the company positions itself against them, when the site
  says so.
- The wedge: what is distinctive — the founding insight, the unusual
  technical bet, or the angle in the company's own story.
- Traction: named customers, integrations, or scale figures — ONLY when the
  text states them.

Style:
- Write like a curious analyst whose profile a reader would actually enjoy —
  not a press release. Plain markdown paragraphs separated by blank lines;
  no headings, no bullet lists.
- When the source material is rich, target roughly 350-600 words across 4-7
  paragraphs, and write AT LEAST 4 substantial paragraphs whenever the input
  supports them. When the material is thin, write fewer, shorter paragraphs
  instead of stretching — a two-paragraph honest profile beats a six-
  paragraph padded one.
- No marketing fluff and no empty adjectives ("innovative", "cutting-edge",
  "world-class", "seamless"). Prefer the concrete nouns and verbs the source
  itself uses.
- Do not open with "{company_name} is a company that ..." boilerplate; lead
  with the problem or the product.

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
