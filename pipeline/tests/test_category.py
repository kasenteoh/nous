"""Tests for the canonical primary_category taxonomy + normalizer (Task 6.1).

The pure-function tests run everywhere. The normalize-taxonomy stage tests are
DB-gated on DATABASE_URL, like the other integration tests.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline.normalize_taxonomy import run_normalize_taxonomy
from nous.util.category import (
    CANONICAL_CATEGORIES,
    normalize_category,
)


def test_canonical_values_pass_through_unchanged() -> None:
    for canon in CANONICAL_CATEGORIES:
        assert normalize_category(canon) == canon


def test_normalize_category_collapses_variants() -> None:
    # The headline assertion from the plan: ad-tech and "Advertising
    # Technology" must land in the same canonical bucket.
    assert normalize_category("ad-tech") == normalize_category(
        "Advertising Technology"
    )


def test_case_and_separator_variants_collapse() -> None:
    assert normalize_category("AI Infrastructure") == "AI infrastructure"
    assert normalize_category("ai-infrastructure") == "AI infrastructure"
    assert normalize_category("developer_tools") == "developer tools"
    assert normalize_category("Vertical SaaS") == "vertical SaaS"


def test_synonym_clusters_merge_to_one_bucket() -> None:
    # "biotech tooling" is one of the prompt's own example buckets — it must
    # fold into the biotech canonical rather than persisting as its own.
    assert normalize_category("biotech tooling") == "biotech"
    # devtools spellings collapse.
    for v in ("devtools", "dev tools", "developer tooling"):
        assert normalize_category(v) == "developer tools"
    # health spellings collapse.
    for v in ("health tech", "healthtech", "digital health"):
        assert normalize_category(v) == "healthcare"


def test_unknown_value_passes_through_trimmed() -> None:
    # Better an un-canonicalised bucket than a lost one — mirror industry.
    assert normalize_category("  underwater welding  ") == "underwater welding"


def test_blank_and_none_become_none() -> None:
    assert normalize_category(None) is None
    assert normalize_category("   ") is None


# ---------------------------------------------------------------------------
# normalize-taxonomy stage (DB-gated)
# ---------------------------------------------------------------------------

_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(*, slug: str, primary_category: str | None) -> Company:
    return Company(
        name=slug.replace("-", " ").title(),
        slug=slug,
        normalized_name=slug.replace("-", " "),
        primary_category=primary_category,
    )


async def _category_for(db: AsyncSession, slug: str) -> str | None:
    row = (
        await db.execute(
            select(Company.primary_category)
            .where(Company.slug == slug)
            .execution_options(populate_existing=True)
        )
    ).first()
    assert row is not None
    return row[0]


@_db
async def test_stage_canonicalizes_variants_in_place(db: AsyncSession) -> None:
    # Two spellings of the same concept + an unknown value + an already-canonical
    # value. After the run the variants collapse, the unknown is untouched, and
    # the canonical one is unchanged.
    db.add_all(
        [
            _make_company(slug="cat-adtech", primary_category="ad-tech"),
            _make_company(
                slug="cat-advertising",
                primary_category="Advertising Technology",
            ),
            _make_company(slug="cat-unknown", primary_category="underwater welding"),
            _make_company(slug="cat-canon", primary_category="developer tools"),
            _make_company(slug="cat-null", primary_category=None),
        ]
    )
    await db.flush()

    summary = await run_normalize_taxonomy(db)
    await db.flush()

    assert await _category_for(db, "cat-adtech") == "sales & marketing tech"
    assert await _category_for(db, "cat-advertising") == "sales & marketing tech"
    assert await _category_for(db, "cat-unknown") == "underwater welding"
    assert await _category_for(db, "cat-canon") == "developer tools"
    assert await _category_for(db, "cat-null") is None

    # Both ad-tech spellings were distinct values that changed; the canonical and
    # unknown ones did not (the null one is never selected).
    assert summary.values_changed >= 2
    assert summary.rows_updated >= 2


@_db
async def test_stage_is_idempotent(db: AsyncSession) -> None:
    db.add_all(
        [
            _make_company(slug="cat-idem-a", primary_category="biotech tooling"),
            _make_company(slug="cat-idem-b", primary_category="devtools"),
        ]
    )
    await db.flush()

    first = await run_normalize_taxonomy(db)
    await db.flush()
    assert first.rows_updated >= 2
    assert await _category_for(db, "cat-idem-a") == "biotech"
    assert await _category_for(db, "cat-idem-b") == "developer tools"

    # Second run: everything is already canonical, so nothing changes.
    second = await run_normalize_taxonomy(db)
    await db.flush()
    assert second.values_changed == 0
    assert second.rows_updated == 0
    # Values remain canonical (fixed point).
    assert await _category_for(db, "cat-idem-a") == "biotech"
    assert await _category_for(db, "cat-idem-b") == "developer tools"
