"""Unit tests for the per-company completeness score (pure, no DB)."""

from __future__ import annotations

from nous.util.completeness import (
    FIELD_WEIGHTS,
    CompletenessFields,
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
