r"""Founder-background / career-history extraction prompt (talent-flow rider).

Input: a company's name, its known leadership roster (the ``people`` rows
enrich already extracted, as name + role), and the concatenated visible text of
its scraped pages. Output: for each named founder/exec, the *prior* companies
they worked at before this one ("ex-Stripe", "previously at Google") — with the
prior role and years when the text states them, and an **empty list otherwise**.

This is the LLM half of the talent-flow "founder background" rider. The #184
`career-history-probe` measured that only ~13–18% of companies name a pedigree,
so the dominant-correct output for the other ~85% is an EMPTY extraction. The
prompt is therefore hardened, mirroring ``hq_country.py`` /
``company_description.py``, to:

- attribute prior roles ONLY to the company's OWN founders/execs — the supplied
  roster is the allow-list (the stage additionally roster-gates the result);
- IGNORE advisors, investors, board members, customers, testimonials, partners,
  and press mentions — the same false-positive class that plagues people/HQ
  extraction (a bio page is full of "backed by ex-Sequoia partners");
- copy prior-company names VERBATIM from the text, never normalize or expand;
- return an empty ``prior_roles`` list (and drop the person entirely) rather
  than guess — unknown pedigree stays unknown, unknown years stay null.

No provider SDK is imported here — the stage sends ``build_prompt`` through
``nous.llm.client.complete_json`` like every other prompt.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field, field_validator, model_validator

# Version stamped onto rows whose content this prompt produced (career_moves via
# extract-career-history, once the persisting stage lands). Scheme
# "<date>.<same-day-counter>"; bump on ANY semantic change to the template,
# schema, or validators — even a wording tweak — so data from a bad revision can
# be found and re-extracted (mirrors the enrichment/eligibility version stamps).
PROMPT_VERSION: str = "2026-07-13.1"


def _clean(value: str | None) -> str | None:
    """Trim whitespace; collapse an empty/blank string to None."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


class PriorRole(BaseModel):
    """One prior employer for a founder/exec, before their current company.

    ``company`` is the only required field — a pedigree mention that names no
    prior employer carries no talent-flow signal and is dropped upstream.
    ``role``/years are best-effort: null when the text doesn't state them.
    """

    company: str = Field(
        ...,
        description=(
            "The prior company/employer name, copied VERBATIM from the text "
            "(e.g. 'Stripe', 'Google', 'McKinsey & Company'). Do not normalize, "
            "expand, or invent — only names the text actually states. Use null "
            "if you cannot name the employer; this role is then dropped."
        ),
    )

    @field_validator("company", mode="before")
    @classmethod
    def _tolerate_null_company(cls, value: object) -> object:
        # complete_json's system prompt globally tells the model to "use null
        # for fields you can't determine", so a JSON ``company: null`` can slip
        # through. Coerce it to "" (rather than let the required-str field raise
        # and discard the WHOLE company's extraction on retry) — the parent
        # PersonCareer validator then drops just this empty-company role.
        return "" if value is None else value
    role: str | None = Field(
        default=None,
        description=(
            "The person's role/title at that prior company, as stated "
            "(e.g. 'Engineer', 'VP of Product', 'early employee'). Null when "
            "the text names the employer but not the role — never guess."
        ),
    )
    start_year: int | None = Field(
        default=None,
        description="4-digit year they joined the prior company, if stated. Null otherwise.",
    )
    end_year: int | None = Field(
        default=None,
        description="4-digit year they left the prior company, if stated. Null otherwise.",
    )

    @model_validator(mode="after")
    def _normalize_fields(self) -> PriorRole:
        # Trim the verbatim name and null-out an empty role. A blank company is
        # not repairable here (the field is required); the parent model drops
        # roles whose company cleans to empty.
        object.__setattr__(self, "company", (_clean(self.company) or ""))
        object.__setattr__(self, "role", _clean(self.role))
        # Guard against a stray non-year integer (e.g. a headcount) leaking into
        # a year field — keep only plausible 4-digit calendar years.
        if self.start_year is not None and not (1900 <= self.start_year <= 2100):
            object.__setattr__(self, "start_year", None)
        if self.end_year is not None and not (1900 <= self.end_year <= 2100):
            object.__setattr__(self, "end_year", None)
        return self


class PersonCareer(BaseModel):
    """A single founder/exec and the prior employers named for them."""

    name: str = Field(
        ...,
        description=(
            "Full name of the founder/exec, matching the supplied roster. Only "
            "the company's OWN leadership — never advisors, investors, board "
            "members, customers, or testimonial names."
        ),
    )
    prior_roles: list[PriorRole] = Field(
        default_factory=list,
        description=(
            "Companies this person worked at BEFORE the current one. Empty list "
            "when the text names no prior employer for them — do NOT fabricate."
        ),
    )

    @model_validator(mode="after")
    def _clean_roles(self) -> PersonCareer:
        object.__setattr__(self, "name", (_clean(self.name) or ""))
        # Drop roles whose (verbatim) prior-company name cleaned to empty, then
        # de-duplicate by (company, role) case-insensitively so a page that
        # repeats "ex-Google" twice yields one edge.
        seen: set[tuple[str, str]] = set()
        kept: list[PriorRole] = []
        for pr in self.prior_roles:
            if not pr.company:
                continue
            key = (pr.company.lower(), (pr.role or "").lower())
            if key in seen:
                continue
            seen.add(key)
            kept.append(pr)
        object.__setattr__(self, "prior_roles", kept)
        return self


class CareerHistoryExtraction(BaseModel):
    """The full per-company extraction: founders/execs with prior employers.

    Empty-not-fabricate is the design centre — for the ~85% of companies whose
    scraped bios name no pedigree, the correct output is ``people == []``.
    """

    people: list[PersonCareer] = Field(
        default_factory=list,
        description=(
            "One entry per company founder/exec for whom the text names ≥1 "
            "prior employer. Empty list when no pedigree is stated."
        ),
    )

    @model_validator(mode="after")
    def _drop_empty_people(self) -> CareerHistoryExtraction:
        # A person carries talent-flow signal only via ≥1 prior role; drop the
        # nameless and the pedigree-less so the extraction holds only real edges.
        object.__setattr__(
            self,
            "people",
            [p for p in self.people if p.name and p.prior_roles],
        )
        return self


PROMPT_TEMPLATE = """\
You extract the PRIOR career history of a software company's own founders and
executives — the companies they worked at BEFORE this one — for a talent-flow
feature. You will read text scraped from the company's public website (homepage
+ about / team / leadership pages) and its known leadership roster.

For EACH person on the roster below, list the companies they worked at BEFORE
{company_name}, exactly as the text states them.

Critical rules:
- Consider ONLY the people in the roster below — they are {company_name}'s own
  founders and leadership. Do NOT add anyone else.
- IGNORE prior-company mentions that belong to advisors, investors, board
  members, customers, testimonials, partners, integrations, or press quotes.
  A bio page is full of "backed by ex-Sequoia partners" and "trusted by teams
  from Google" — those are NOT this company's leadership's own history.
- A "prior role" is a job the person held at a DIFFERENT company BEFORE
  {company_name}. Do NOT list {company_name} itself. Do NOT list schools,
  degrees, or awards — only employers.
- Copy each prior-company name VERBATIM from the text. Do not normalize,
  expand abbreviations, or infer a fuller legal name.
- `role`: the person's title at that prior company, only if stated. Null
  otherwise — never guess a title.
- `start_year` / `end_year`: 4-digit years, only if the text states them. Null
  otherwise.
- If the text names NO prior employer for a person, give them an empty
  `prior_roles` list (they will be dropped). If the text names no prior
  employer for ANYONE, return an empty `people` list. Returning empty is the
  correct, common answer — never invent a pedigree to fill the schema.

Company name: {company_name}

Known leadership roster (attribute prior roles ONLY to these people):
{roster}

Website text (home / about / team / leadership; may be truncated):
---
{cleaned_text}
---

Return JSON only.
"""


def _format_roster(roster: Sequence[tuple[str, str]]) -> str:
    """Render the (name, role) roster as a bulleted allow-list for the prompt."""
    if not roster:
        return "(none provided)"
    return "\n".join(f"- {name} — {role}" for name, role in roster)


def build_prompt(
    *,
    company_name: str,
    roster: Sequence[tuple[str, str]],
    cleaned_text: str,
) -> str:
    """Build the career-history extraction prompt.

    ``roster`` is the company's known leadership as ``(name, role)`` pairs (the
    enrich ``people`` rows) — the allow-list the model must attribute against.
    """
    return PROMPT_TEMPLATE.format(
        company_name=company_name,
        roster=_format_roster(roster),
        cleaned_text=cleaned_text,
    )
