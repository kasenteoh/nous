"""Per-company completeness scoring — a pure 0..1 weighted-field primitive.

The data-quality report aggregates this into a distribution; later it feeds
husk-enrichment ordering and a public trust badge (ROADMAP). Weights sum to 1.0
and are tuned so the husk-defining fields (website, description) dominate — a
company with only a name scores ~0 (a husk), one with every field scores 1.0.
It is a *relative* signal for prioritisation, not a precise percentage; retune
:data:`FIELD_WEIGHTS` to reprioritise. Pure and side-effect-free so it is unit-
testable and safe to call per-row.
"""

from __future__ import annotations

from pydantic import BaseModel

# Field weights, summing to 1.0. Husk-defining fields (website, description)
# dominate; the long tail (logo, tags, employees) contributes a little polish.
FIELD_WEIGHTS: dict[str, float] = {
    "has_website": 0.20,
    "has_description": 0.20,
    "has_funding": 0.15,
    "has_location": 0.10,
    "has_industry": 0.10,
    "has_people": 0.10,
    "has_logo": 0.05,
    "has_tags": 0.05,
    "has_employees": 0.05,
}


class CompletenessFields(BaseModel):
    """Presence flags for a company's completeness-scored fields."""

    has_website: bool = False
    has_description: bool = False
    has_funding: bool = False  # ≥1 funding round
    has_location: bool = False  # hq_country or hq_city
    has_industry: bool = False
    has_people: bool = False  # ≥1 person
    has_logo: bool = False
    has_tags: bool = False  # non-empty tags array
    has_employees: bool = False  # an employee-count range


def completeness_score(fields: CompletenessFields) -> float:
    """Weighted 0..1 completeness score. 0 = husk (nothing), 1 = fully complete."""
    present = fields.model_dump()
    return round(sum(w for key, w in FIELD_WEIGHTS.items() if present[key]), 4)
