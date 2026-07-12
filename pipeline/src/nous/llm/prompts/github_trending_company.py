"""Company-judgment prompt for the discover-github-trending stage.

Input: a GitHub owner (org/user login, profile name/bio/website when the API
supplied them) plus its trending repos (name, description, language, stars).
Output: is this plausibly a US software COMPANY with a product — not a
personal project, foundation, university lab, or big-tech sponsor? Only
accepted owners flow to the auto-create path; ``null`` means "not confident",
and uncertainty must never be resolved by guessing.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Version stamped alongside rows whose creation this prompt gated (recorded in
# the stage summary; companies created via this gate carry
# discovered_via='github_trending'). Scheme: "<date>.<same-day-counter>".
# Bump on ANY semantic change to the template, schema, or validators — even a
# wording tweak — so data from a bad revision can be found and re-run.
PROMPT_VERSION: str = "2026-07-11.1"


class TrendingCompanyJudgment(BaseModel):
    is_company: bool | None = Field(
        default=None,
        description=(
            "True when this GitHub owner is plausibly a private, independent "
            "US-plausible SOFTWARE COMPANY with a commercial product (the repo "
            "is, or supports, something the company sells or hosts). False "
            "when it clearly is NOT one: an individual's personal project, a "
            "community/volunteer open-source project, a foundation or "
            "nonprofit, a university or research lab, a government body, a "
            "big-tech corporation or its sponsored project (Google, Microsoft, "
            "Meta, Amazon, Apple, etc.), or a clearly non-US company. Null "
            "when the evidence does not support a confident call — never "
            "guess."
        ),
    )
    company_name: str | None = Field(
        default=None,
        description=(
            "The company's proper display name, ONLY when is_company is true. "
            "Derive it from the provided login/profile name (fix casing or "
            "spacing, drop a '-inc' style suffix); NEVER invent a name that "
            "does not correspond to the given owner."
        ),
    )
    reason: str | None = Field(
        default=None,
        description="One short factual sentence supporting the judgment.",
    )


PROMPT_TEMPLATE = """\
You are curating a discovery catalog of US software startups. A GitHub
account owns one or more repositories trending on GitHub today. Decide
whether the account plausibly belongs to a US software COMPANY whose
product this repo is or supports.

Rules:
- `is_company`: true ONLY when the owner is plausibly a private, independent
  software COMPANY with a commercial product — a startup or scale-up that
  sells, hosts, or commercially supports the software (open-core, SaaS with
  an open-source engine, a devtool with a hosted cloud, etc.). It does not
  need to be provably US-based from this evidence alone — plausibly US (or
  ambiguous) is acceptable; a later pipeline stage verifies headquarters.
  Set it FALSE when the evidence clearly shows one of these instead:
    • an individual's personal account or side project;
    • a community / volunteer open-source project with no company behind it;
    • a foundation, nonprofit, or standards body (e.g. Apache, Linux
      Foundation, CNCF projects without a single company owner);
    • a university, research lab, or government body;
    • a big-tech corporation or its sponsored project (Google, Microsoft,
      Meta, Amazon, Apple, NVIDIA, and similar established giants — they are
      not startups and must not enter the catalog);
    • a clearly non-US company (e.g. an organization whose profile or repos
      state a non-US headquarters).
  When the evidence does not support a confident call, return null. Never
  guess — null is always safer than a wrong true.
- `company_name`: only when is_company is true. Restyle the given login or
  profile name into the company's proper display name (casing, spacing,
  dropping corporate suffixes). NEVER return a name that is not derived from
  the owner shown below.
- `reason`: one short factual sentence.

GitHub owner login: {owner_login}
Account type: {account_type}
Profile name: {profile_name}
Profile website: {profile_website}
Profile bio:
---
{profile_bio}
---

Trending repositories owned by this account:
{repos_block}

Return JSON only.
"""

# Rendered in place of missing profile fields so the model sees an explicit
# "we don't know" instead of an empty string it might over-interpret.
_UNKNOWN = "(unknown)"


def format_repos_block(
    repos: list[tuple[str, str | None, str | None, int | None]],
) -> str:
    """Render (name, description, language, stars) tuples as prompt lines."""
    lines: list[str] = []
    for name, description, language, stars in repos:
        details = [
            f"language: {language}" if language else None,
            f"stars: {stars}" if stars is not None else None,
        ]
        suffix = f" [{', '.join(d for d in details if d)}]" if any(details) else ""
        lines.append(f"- {name}: {description or '(no description)'}{suffix}")
    return "\n".join(lines)


def build_prompt(
    *,
    owner_login: str,
    account_type: str | None,
    profile_name: str | None,
    profile_website: str | None,
    profile_bio: str | None,
    repos_block: str,
) -> str:
    return PROMPT_TEMPLATE.format(
        owner_login=owner_login,
        account_type=account_type or _UNKNOWN,
        profile_name=profile_name or _UNKNOWN,
        profile_website=profile_website or _UNKNOWN,
        profile_bio=profile_bio or _UNKNOWN,
        repos_block=repos_block,
    )
