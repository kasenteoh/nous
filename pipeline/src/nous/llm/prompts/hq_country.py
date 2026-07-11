"""Focused HQ-country inference prompt for the infer-hq-country repair stage.

Input: a company's name + description + the visible text of its own
address-bearing pages (about/contact/legal/imprint/privacy) plus stored
homepage text. Output: the company's own HQ country as an ISO-3166 alpha-2
code, with the verbatim snippet that proves it — or null when the text does
not state the company's own location.

The dominant false-positive on these pages is customer/testimonial/investor
names (a support-tool homepage is wall-to-wall foreign customer logos). The
prompt is hardened to judge ONLY the company's own headquarters and to quote
the exact supporting text, so the caller can verify the quote is real.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Version stamped onto rows whose content this prompt produced (companies
# hq_country via infer-hq-country). Scheme: "<date>.<same-day-counter>". Bump
# on ANY semantic change to the template, schema, or validators — even a
# wording tweak — so data from a bad revision can be found and re-run.
PROMPT_VERSION: str = "2026-07-10.1"


class HqCountryJudgment(BaseModel):
    hq_country: str | None = Field(
        default=None,
        description=(
            "The COMPANY'S OWN headquarters country as a 2-letter ISO-3166 "
            "alpha-2 code (e.g. 'US', 'DK', 'GB', 'DE'). Set this ONLY when the "
            "text states the company's own location — a registered address, a "
            "formal HQ/office line, a legal entity (e.g. 'Acme ApS', 'Acme "
            "GmbH'), or 'based in <city>' for the company itself. Null when the "
            "text is silent or ambiguous about the company's OWN location — "
            "never guess."
        ),
    )
    evidence_quote: str | None = Field(
        default=None,
        description=(
            "The exact, verbatim snippet from the supplied text that proves "
            "hq_country (copy it word-for-word; do not paraphrase). Null when "
            "hq_country is null."
        ),
    )


PROMPT_TEMPLATE = """\
You are verifying the headquarters country of ONE company for a catalog of
US-headquartered software startups.

Decide the company's OWN headquarters country from the text below.

Critical rules:
- Judge ONLY this company's own headquarters. IGNORE the locations of
  customers, testimonials, reviewers, logos, investors, partners, and
  integrations — a page full of foreign customer names does NOT make the
  company foreign.
- Set `hq_country` (2-letter ISO code) ONLY when the text states the company's
  own location: a registered/office address, a formal HQ line, a legal entity
  suffix (ApS, GmbH, Oy, AB, B.V., Pty Ltd, S.r.l., Ltd, ...), or 'based in
  <city>' for the company itself.
- `evidence_quote`: copy the EXACT snippet that proves it, word-for-word. Do
  not paraphrase. If you cannot quote it verbatim, return null.
- If the text does not clearly state the company's OWN location, return null
  for both fields. Never guess. Unknown stays unknown.

Company name: {company_name}

Stored description:
---
{description}
---

Website text (about / contact / legal / imprint / privacy / home; may be truncated):
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
