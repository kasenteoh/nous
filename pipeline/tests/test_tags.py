"""Tests for the canonical tag vocabulary + canonicalizer (H-2).

The pure-function tests run everywhere. The normalize-taxonomy tags-pass
tests are DB-gated on DATABASE_URL, like the other integration tests.
"""

from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline.normalize_taxonomy import run_normalize_taxonomy
from nous.util.tags import (
    _ALIAS_SOURCES,
    CANONICAL_TAGS,
    canonicalize_tag,
    canonicalize_tags,
)

_LOWER_HYPHEN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def test_vocabulary_size_in_target_band() -> None:
    # The plan targets a deliberately generic ~60–120 canonical tags.
    assert 60 <= len(CANONICAL_TAGS) <= 120


def test_canonical_tags_are_lowercase_hyphenated_and_unique() -> None:
    assert len(set(CANONICAL_TAGS)) == len(CANONICAL_TAGS)
    for tag in CANONICAL_TAGS:
        assert _LOWER_HYPHEN.match(tag), f"{tag!r} is not lowercase-hyphenated"


def test_canonical_tags_are_fixed_points() -> None:
    for tag in CANONICAL_TAGS:
        assert canonicalize_tag(tag) == tag


def test_every_alias_maps_to_its_canonical_and_converges() -> None:
    for canon, variants in _ALIAS_SOURCES.items():
        assert canon in CANONICAL_TAGS, f"alias target {canon!r} not canonical"
        for variant in variants:
            mapped = canonicalize_tag(variant)
            assert mapped == canon, f"{variant!r} -> {mapped!r}, want {canon!r}"
            # Idempotence: applying the map twice changes nothing (the
            # normalize-taxonomy convergence guarantee rests on this).
            assert canonicalize_tag(mapped) == mapped


def test_live_recording_mismatch_pairs_collapse() -> None:
    # Both sides of every live-eval mismatch the plan cites land on one tag.
    assert canonicalize_tag("ci-observability") == "ci-cd"
    assert canonicalize_tag("payment-routing") == "payments"
    assert canonicalize_tag("wholesale-marketplace") == "marketplace"


def test_case_and_separator_variants_collapse() -> None:
    assert canonicalize_tag("CI/CD") == "ci-cd"
    assert canonicalize_tag("Open Source") == "open-source"
    assert canonicalize_tag("developer_tools") == "devtools"
    assert canonicalize_tag("Machine Learning") == "ml"


def test_unknown_tags_pass_through_mechanically_normalized() -> None:
    # The map consolidates, it doesn't gate: unknown tags survive in the
    # historical lowercase-hyphenated form (open vocabulary).
    assert canonicalize_tag("dev-boards") == "dev-boards"
    assert canonicalize_tag("HIPAA") == "hipaa"
    assert canonicalize_tag("Quantum Sensing") == "quantum-sensing"


def test_canonicalize_tags_dedupes_preserving_order() -> None:
    # freight folds into logistics (already present) and chat into messaging;
    # first occurrence keeps its position, later collapses vanish.
    assert canonicalize_tags(
        ["logistics", "freight", "supply-chain", "chat", "messaging"]
    ) == ["logistics", "supply-chain", "messaging"]


def test_canonicalize_tags_drops_blanks_and_keeps_unknowns() -> None:
    assert canonicalize_tags(["", "  ", "ai", "journaling"]) == [
        "ai",
        "journaling",
    ]
    assert canonicalize_tags([]) == []


def test_golden_fixture_near_duplicates_collapse() -> None:
    # The internal near-duplicates observed across the golden fixtures.
    assert canonicalize_tag("marketplaces") == "marketplace"
    assert canonicalize_tag("developer-tools") == "devtools"
    assert canonicalize_tag("payment-orchestration") == "payments"
    assert canonicalize_tag("payouts") == "payments"
    assert canonicalize_tag("listings") == "directory"
    assert canonicalize_tag("on-call") == "incident-management"
    assert canonicalize_tag("cloud-cost") == "finops"


# ---------------------------------------------------------------------------
# normalize-taxonomy tags pass (DB-gated)
# ---------------------------------------------------------------------------

_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(*, slug: str, tags: list[str] | None) -> Company:
    return Company(
        name=slug.replace("-", " ").title(),
        slug=slug,
        normalized_name=slug.replace("-", " "),
        tags=tags,
    )


async def _tags_for(db: AsyncSession, slug: str) -> list[str] | None:
    row = (
        await db.execute(
            select(Company.tags)
            .where(Company.slug == slug)
            .execution_options(populate_existing=True)
        )
    ).first()
    assert row is not None
    return row[0]


@_db
async def test_stage_recanonicalizes_tags_in_place(db: AsyncSession) -> None:
    db.add_all(
        [
            _make_company(
                slug="tags-sprawl",
                tags=["ci-observability", "developer-tools", "saas"],
            ),
            _make_company(
                slug="tags-dupes",
                tags=["payments", "payment-routing", "fintech"],
            ),
            _make_company(slug="tags-unknown", tags=["dev-boards", "hipaa"]),
            _make_company(slug="tags-canon", tags=["ai", "devtools"]),
            _make_company(slug="tags-null", tags=None),
            _make_company(slug="tags-empty", tags=[]),
        ]
    )
    await db.flush()

    summary = await run_normalize_taxonomy(db)
    await db.flush()

    assert await _tags_for(db, "tags-sprawl") == ["ci-cd", "devtools", "saas"]
    # payment-routing collapses onto the already-present payments (dedupe).
    assert await _tags_for(db, "tags-dupes") == ["payments", "fintech"]
    assert await _tags_for(db, "tags-unknown") == ["dev-boards", "hipaa"]
    assert await _tags_for(db, "tags-canon") == ["ai", "devtools"]
    assert await _tags_for(db, "tags-null") is None
    assert await _tags_for(db, "tags-empty") == []

    assert summary.tags.values_changed >= 2
    assert summary.tags.rows_updated >= 2


@_db
async def test_tags_pass_converges_second_run_rewrites_zero(
    db: AsyncSession,
) -> None:
    db.add_all(
        [
            _make_company(
                slug="tags-idem-a",
                tags=["wholesale-marketplace", "food", "restaurants"],
            ),
            _make_company(slug="tags-idem-b", tags=["on-call", "sre", "devops"]),
        ]
    )
    await db.flush()

    first = await run_normalize_taxonomy(db)
    await db.flush()
    assert first.tags.rows_updated >= 2
    assert await _tags_for(db, "tags-idem-a") == [
        "marketplace",
        "food",
        "restaurants",
    ]
    assert await _tags_for(db, "tags-idem-b") == [
        "incident-management",
        "sre",
        "devops",
    ]

    # Convergence: the second run finds every tag list already canonical.
    second = await run_normalize_taxonomy(db)
    await db.flush()
    assert second.tags.values_changed == 0
    assert second.tags.rows_updated == 0
