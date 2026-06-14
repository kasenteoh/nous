"""Tests for the canonical industry taxonomy + normalizer (M1)."""

from __future__ import annotations

from nous.util.industry import CANONICAL_INDUSTRIES, normalize_industry


def test_canonical_values_pass_through_unchanged() -> None:
    for canon in CANONICAL_INDUSTRIES:
        assert normalize_industry(canon) == canon


def test_case_and_separator_variants_collapse() -> None:
    assert normalize_industry("AI Infrastructure") == "AI infrastructure"
    assert normalize_industry("ai-infrastructure") == "AI infrastructure"
    assert normalize_industry("vertical_saas") == "vertical SaaS"


def test_synonym_clusters_merge_to_one_bucket() -> None:
    # The exact sprawl the QA found: ad-tech / adtech / advertising technology.
    for v in ("ad-tech", "adtech", "advertising technology"):
        assert normalize_industry(v) == "sales & marketing tech"
    # climate tech / climate-tech / cleantech / clean energy.
    for v in ("climate tech", "climate-tech", "cleantech", "clean energy"):
        assert normalize_industry(v) == "climate & energy"
    # biotech / biotechnology.
    assert normalize_industry("biotechnology") == "biotech"
    # AI variants fold into the AI bucket.
    assert normalize_industry("AI research") == "AI infrastructure"


def test_unknown_value_passes_through_trimmed() -> None:
    # Better an un-canonicalised bucket than a lost one.
    assert normalize_industry("  underwater welding  ") == "underwater welding"


def test_blank_and_none_become_none() -> None:
    assert normalize_industry(None) is None
    assert normalize_industry("   ") is None
