"""Unit tests for the per-company completeness score (pure, no DB)."""

from __future__ import annotations

from nous.util.completeness import (
    FIELD_WEIGHTS,
    CompletenessFields,
    completeness_fields,
    completeness_score,
)


def test_weights_sum_to_one() -> None:
    assert round(sum(FIELD_WEIGHTS.values()), 6) == 1.0


def test_empty_company_scores_zero() -> None:
    assert completeness_score(CompletenessFields()) == 0.0


def test_fully_complete_scores_one() -> None:
    fields = CompletenessFields(
        has_website=True,
        has_description=True,
        has_funding=True,
        has_location=True,
        has_industry=True,
        has_people=True,
        has_logo=True,
        has_tags=True,
        has_employees=True,
    )
    assert completeness_score(fields) == 1.0


def test_website_and_description_dominate() -> None:
    """The two husk-defining fields alone are 40% of the score."""
    fields = CompletenessFields(has_website=True, has_description=True)
    assert completeness_score(fields) == 0.40


def test_partial_score_is_weighted_sum() -> None:
    fields = CompletenessFields(has_website=True, has_funding=True, has_logo=True)
    # 0.20 + 0.15 + 0.05
    assert completeness_score(fields) == 0.40


# ---------------------------------------------------------------------------
# completeness_fields — the shared raw-value -> flags mapping (single source of
# truth for both the data-quality report and the stored score).
# ---------------------------------------------------------------------------


def _all_absent() -> dict[str, object]:
    """Kwargs for a bare husk (every field absent)."""
    return {
        "website": None,
        "description_short": None,
        "funding_round_count": 0,
        "hq_country": None,
        "hq_city": None,
        "industry_group": None,
        "has_people": False,
        "logo_url": None,
        "tags": None,
        "employee_count_min": None,
        "employee_count_max": None,
    }


def test_completeness_fields_all_absent_is_husk() -> None:
    fields = completeness_fields(**_all_absent())  # type: ignore[arg-type]
    assert fields == CompletenessFields()
    assert completeness_score(fields) == 0.0


def test_completeness_fields_all_present_is_complete() -> None:
    fields = completeness_fields(
        website="https://x.com/",
        description_short="Does things.",
        funding_round_count=2,
        hq_country="US",
        hq_city="SF",
        industry_group="AI",
        has_people=True,
        logo_url="https://x.com/logo.png",
        tags=["ai"],
        employee_count_min=10,
        employee_count_max=50,
    )
    assert completeness_score(fields) == 1.0


def test_completeness_fields_location_from_either_city_or_country() -> None:
    country_only = completeness_fields(**{**_all_absent(), "hq_country": "US"})  # type: ignore[arg-type]
    city_only = completeness_fields(**{**_all_absent(), "hq_city": "SF"})  # type: ignore[arg-type]
    assert country_only.has_location is True
    assert city_only.has_location is True


def test_completeness_fields_empty_tags_and_zero_funding_are_absent() -> None:
    """An empty tags array and a 0 funding_round_count read as absent, matching
    the data-quality report's SQL cardinality/`> 0` semantics."""
    fields = completeness_fields(**{**_all_absent(), "tags": [], "funding_round_count": 0})  # type: ignore[arg-type]
    assert fields.has_tags is False
    assert fields.has_funding is False


def test_completeness_fields_employees_from_either_bound() -> None:
    min_only = completeness_fields(**{**_all_absent(), "employee_count_min": 5})  # type: ignore[arg-type]
    max_only = completeness_fields(**{**_all_absent(), "employee_count_max": 5})  # type: ignore[arg-type]
    assert min_only.has_employees is True
    assert max_only.has_employees is True
