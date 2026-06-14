"""Tests for the canonical industry taxonomy + normalizer (M1, expanded)."""

from __future__ import annotations

from nous.util.industry import CANONICAL_INDUSTRIES, normalize_industry


def test_canonical_values_pass_through_unchanged() -> None:
    for canon in CANONICAL_INDUSTRIES:
        assert normalize_industry(canon) == canon


def test_canonical_values_are_fixed_points() -> None:
    # Every bucket must normalize to itself, so applying the stage twice is a
    # no-op (the idempotency guarantee the stage relies on).
    for canon in CANONICAL_INDUSTRIES:
        once = normalize_industry(canon)
        assert once is not None
        assert normalize_industry(once) == once


def test_case_and_separator_variants_collapse() -> None:
    assert normalize_industry("AI Infrastructure") == "AI infrastructure"
    assert normalize_industry("ai-infrastructure") == "AI infrastructure"
    assert normalize_industry("vertical_saas") == "vertical SaaS"


def test_healthcare_sprawl_collapses_to_one_bucket() -> None:
    # The exact sprawl the QA found: five separate dropdown entries for one idea.
    for v in (
        "healthcare",
        "healthtech",
        "healthcare technology",
        "healthcare software",
        "healthcare AI",
        "digital health",
        "health & wellness",
    ):
        assert normalize_industry(v) == "healthcare"


def test_adtech_sprawl_collapses_to_one_bucket() -> None:
    for v in ("ad-tech", "adtech", "advertising technology", "advertising"):
        assert normalize_industry(v) == "sales & marketing tech"


def test_synonym_clusters_merge_to_one_bucket() -> None:
    # climate tech / climate-tech / cleantech / clean energy.
    for v in ("climate tech", "climate-tech", "cleantech", "clean energy"):
        assert normalize_industry(v) == "climate & energy"
    # biotech / biotechnology.
    assert normalize_industry("biotechnology") == "biotech"
    # AI variants fold into the AI bucket.
    assert normalize_industry("AI research") == "AI infrastructure"
    # e-commerce / ecommerce / ecommerce SaaS → one bucket.
    for v in ("e-commerce", "ecommerce", "ecommerce SaaS", "retail"):
        assert normalize_industry(v) == "e-commerce & retail"


def test_new_buckets_absorb_long_tail() -> None:
    # The 14 added buckets each pull a swathe of singletons out of the dropdown.
    assert normalize_industry("defense technology") == "defense & aerospace"
    assert normalize_industry("space infrastructure") == "defense & aerospace"
    assert normalize_industry("industrial automation") == "manufacturing & industrial"
    assert normalize_industry("food delivery") == "food & beverage"
    assert normalize_industry("entertainment") == "media & entertainment"
    assert normalize_industry("govtech") == "government & public sector"
    assert normalize_industry("electric vehicles") == "transportation & mobility"
    assert normalize_industry("agtech") == "agtech"
    assert normalize_industry("identity verification") == "identity & fraud"
    assert normalize_industry("cloud infrastructure") == "enterprise infrastructure"


def test_separator_only_variant_of_existing_bucket_maps() -> None:
    # "prop-tech" keys to "prop tech", which does NOT equal the "proptech" key —
    # it needs an explicit alias, which the expansion adds.
    assert normalize_industry("prop-tech") == "proptech"


def test_unknown_value_passes_through_trimmed() -> None:
    # Better an un-canonicalised bucket than a lost one. "diving equipment" is a
    # real prod long-tail value left intentionally un-bucketed.
    assert normalize_industry("  underwater welding  ") == "underwater welding"
    assert normalize_industry("diving equipment") == "diving equipment"


def test_blank_and_none_become_none() -> None:
    assert normalize_industry(None) is None
    assert normalize_industry("   ") is None
