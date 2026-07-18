"""Article-subject match — ingest-time entity adjudication (DISCRIMINATIVE).

The last layer of the entity-aware ingestion guard (BACKLOG 2026-07-17 P0).
``article_mentions_company`` proves the NAME appears; the cheap
:mod:`nous.util.entity_corroboration` signals prove it appears as a bare
proper noun — but neither can tell edtech-Wonder from food-Wonder when the
article says only "Wonder raises $650M". This prompt adjudicates exactly that
residue: given the company nous KNOWS (name, website, description, industry,
HQ) and the article nous is about to attach, is the article's funded subject
THIS company or a same-named different entity?

Design rules (the moat is trust):

- **Discriminative, never generative.** The model compares two given records.
  It writes no prose and uses no outside knowledge about either entity —
  the decision must rest on the text shown.
- **Wrong-attach is worse than no-attach.** A wrongly attached round poisons
  totals, /trends, and the company page (the bespoke-labs/$1B class); a
  dropped correct article costs one timeline row that usually re-appears
  from another outlet. When the evidence is thin or conflicting the model
  MUST return is_subject=false with low confidence — the guard only
  attaches on is_subject=true AND confidence != 'low'.
- **Name the other entity when visible.** If the article names a fuller or
  different entity ("Primary Wave Music", "Impulse Dynamics"), returning it
  in ``other_entity_name`` makes every drop auditable in the run summary.

Caller: the ingest-news persist path (and, later, the retroactive
high-prominence audit). Sent through ``nous.llm.client.complete_json`` like
every prompt; never import a provider SDK here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Stamped into logs (and any future persisted adjudication rows). Scheme
# "<date>.<same-day-counter>"; bump on ANY semantic change to the template or
# schema so past decisions can be found and re-adjudicated.
PROMPT_VERSION: str = "2026-07-18.1"

# The article excerpt shown to the model. The funded subject is named in the
# headline/lede; a longer excerpt costs tokens without adding subject signal.
EXCERPT_CHARS: int = 1200


class ArticleSubjectMatch(BaseModel):
    """Verdict: is the article's funded subject the given company?"""

    is_subject: bool = Field(
        ...,
        description=(
            "True ONLY when the article's funded/covered subject is the "
            "specific company described in the profile. False when it is a "
            "same-named different entity, a different company entirely, or "
            "when the evidence is too thin to tell."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        ...,
        description=(
            "'high' when independent signals agree (the article names the "
            "company's website/domain, product, founders, or matches the "
            "profile's specific industry AND location); 'medium' when the "
            "match rests on one corroborating signal beyond the shared "
            "name; 'low' when the shared name is the ONLY link or signals "
            "conflict. When unsure, return is_subject=false with 'low'."
        ),
    )
    other_entity_name: str | None = Field(
        default=None,
        description=(
            "When is_subject is false because the article is about a "
            "DIFFERENT entity, the fullest name the article uses for that "
            "entity (e.g. 'Primary Wave Music', 'Impulse Dynamics'); null "
            "when no distinct entity is visible."
        ),
    )


_TEMPLATE = """\
You are checking whether a news article is about a specific company, or about
a DIFFERENT entity that happens to share its name. Company names collide
constantly (a fintech "Wave" vs the music publisher "Primary Wave"; an edtech
"Wonder" vs a food-delivery "Wonder"), and attaching the wrong article would
publish false funding data — so you must be strict.

The company nous tracks:
- Name: {name}
- Website: {website}
- What it does: {description}
- Industry: {industry}
- HQ: {hq}

The article about to be attached to this company:
- Headline: {title}
- Text (excerpt): {excerpt}

Decide whether the article's funded/covered SUBJECT is this specific company.

Rules:
- Judge ONLY from the text above. Do not use outside knowledge about either
  the company or any similarly named entity.
- The shared name alone is NEVER enough. Look for corroboration: does the
  article's description of the subject's business match "What it does"? Does
  it mention the company's website or domain? The same industry? The same
  location? Founders or products consistent with the profile?
- If the article describes a DIFFERENT line of business (a music fund, a
  medical-device maker, a construction firm) than the profile, it is a
  different entity: is_subject=false, and copy the fullest name the article
  uses for its subject into other_entity_name.
- If the evidence is too thin to tell (e.g. a bare headline that only shares
  the name), return is_subject=false with confidence="low". A missed article
  is recoverable; a wrong attachment publishes false data.
- Unknown profile fields are shown as "(unknown)" — treat them as absent
  evidence, not as a match.

Return JSON with fields: is_subject (boolean), confidence ("low" | "medium" |
"high"), other_entity_name (string or null).
"""

_UNKNOWN = "(unknown)"


def _fmt(value: str | None) -> str:
    value = (value or "").strip()
    return value if value else _UNKNOWN


def build_article_subject_match_prompt(
    *,
    name: str,
    website: str | None,
    description: str | None,
    industry: str | None,
    hq: str | None,
    title: str,
    article_text: str,
) -> str:
    """Render the adjudication prompt for one (company, article) pair."""
    return _TEMPLATE.format(
        name=_fmt(name),
        website=_fmt(website),
        description=_fmt(description),
        industry=_fmt(industry),
        hq=_fmt(hq),
        title=_fmt(title),
        excerpt=_fmt(article_text[:EXCERPT_CHARS]),
    )
