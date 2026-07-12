"""Theme-naming prompt for the compute-themes stage (E-3).

Input: an industry_group plus the member companies of one embedding cluster
(name + short description each). Output: a short display name and a
one-sentence description for the market theme the cluster represents — or
``null`` when the members don't share a coherent theme. A null name drops the
cluster entirely (no theme row is written): the catalog never shows a
fabricated segment, per the null-over-fabricate rule.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Version stamped onto themes.prompt_version for rows this prompt named
# (migration 0031 convention). Scheme: "<date>.<same-day-counter>". Bump on
# ANY semantic change to the template, schema, or validators — even a wording
# tweak — so data from a bad revision can be found and re-named.
PROMPT_VERSION: str = "2026-07-11.1"

# Cap on members rendered into the prompt. Clusters are typically 3–50
# companies; 40 one-line entries keeps the prompt well under the shared
# MAX_PROMPT_INPUT_CHARS ceiling while still showing the whole cluster in
# almost every case (a 40-member sample of a larger cluster names it just as
# well).
MAX_MEMBERS: int = 40

# Per-member description cap — enough to convey what the company does without
# letting one long description crowd out the rest of the cluster.
_MAX_DESCRIPTION_CHARS: int = 200


class ThemeNaming(BaseModel):
    """LLM response schema. Both fields null when the cluster is incoherent."""

    name: str | None = Field(
        default=None,
        description=(
            "A 2-5 word display name for the market theme these companies "
            "share, e.g. 'AI code review', 'Payroll for global teams'. Null "
            "when the companies do NOT share one coherent theme — never "
            "invent an umbrella that doesn't fit."
        ),
    )
    description: str | None = Field(
        default=None,
        description=(
            "Exactly one sentence describing what companies in this theme "
            "build, grounded only in the member descriptions shown. Null "
            "whenever name is null."
        ),
    )


PROMPT_TEMPLATE = """\
You are naming market themes for a catalog of US software startups. The
companies below were grouped by the similarity of their product
descriptions, within the "{industry_group}" industry.

Rules:
- `name`: a specific 2-5 word theme name that covers MOST of the members —
  a market segment a reader would recognize (e.g. "AI coding assistants",
  "Fleet telematics", "Compliance automation"). Do not simply restate the
  industry name; the theme must be narrower than "{industry_group}".
- `description`: exactly one sentence describing what companies in this
  theme build. Ground it ONLY in the member descriptions below — do not add
  claims about market size, funding, or customers.
- If the members do NOT share one coherent theme (a grab-bag of unrelated
  products), return null for BOTH fields. Never force an umbrella name onto
  an incoherent group — null is always safer than a wrong name.

Member companies:
{members_block}

Return JSON only.
"""


def format_members_block(
    members: list[tuple[str, str | None]],
) -> str:
    """Render (name, description_short) pairs as prompt lines.

    Caps at MAX_MEMBERS entries and _MAX_DESCRIPTION_CHARS per description;
    a missing description renders an explicit "(no description)" so the
    model can't over-interpret an empty string.
    """
    lines: list[str] = []
    for name, description in members[:MAX_MEMBERS]:
        desc = (description or "").strip()
        if len(desc) > _MAX_DESCRIPTION_CHARS:
            desc = desc[: _MAX_DESCRIPTION_CHARS - 1] + "…"
        lines.append(f"- {name}: {desc or '(no description)'}")
    if len(members) > MAX_MEMBERS:
        lines.append(f"… and {len(members) - MAX_MEMBERS} more similar companies")
    return "\n".join(lines)


def build_prompt(*, industry_group: str, members_block: str) -> str:
    return PROMPT_TEMPLATE.format(
        industry_group=industry_group, members_block=members_block
    )
