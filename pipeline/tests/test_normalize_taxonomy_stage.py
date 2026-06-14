"""DB-gated integration tests for the normalize-taxonomy stage.

Requires DATABASE_URL pointing at a Postgres with the schema at head.

The stage rewrites two free-text columns in place via committed string maps
(no LLM): ``primary_category`` (``normalize_category``) and ``industry_group``
(``normalize_industry``). Coverage here focuses on the newly-added
``industry_group`` rewrite plus the cross-column idempotency guarantee:

- the healthcare sprawl (healthcare / healthtech / healthcare technology /
  healthcare AI) collapses to one ``industry_group`` bucket;
- the ad-tech sprawl (ad-tech / adtech / advertising technology) collapses to
  one bucket;
- a genuinely-unknown ``industry_group`` value is left untouched (passthrough);
- a NULL ``industry_group`` is never overwritten; and
- a second run reports zero changes on BOTH columns (idempotent).
"""

from __future__ import annotations

import os
from collections import Counter

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline.normalize_taxonomy import run_normalize_taxonomy
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(
    name: str,
    *,
    industry_group: str | None = None,
    primary_category: str | None = None,
) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{normalize_name(name) or 'company'}-{suffix}",
        normalized_name=normalize_name(name),
        hq_country="US",
        industry_group=industry_group,
        primary_category=primary_category,
    )


async def _industry_groups(
    session: AsyncSession, ids: list[object]
) -> dict[object, str | None]:
    rows = (
        (
            await session.execute(
                select(Company.id, Company.industry_group).where(
                    Company.id.in_(ids)
                )
            )
        )
        .tuples()
        .all()
    )
    return {cid: ig for cid, ig in rows}


async def test_industry_group_sprawl_collapses_and_unknown_survives(
    db: AsyncSession,
) -> None:
    """Healthcare and ad-tech variants each collapse to a single bucket; a
    genuinely-unknown value and a NULL are left untouched."""
    healthcare_variants = [
        "healthcare",
        "healthtech",
        "healthcare technology",
        "healthcare AI",
        "healthcare software",
    ]
    adtech_variants = ["ad-tech", "adtech", "advertising technology"]

    companies = [
        _make_company(f"Health {i}", industry_group=v)
        for i, v in enumerate(healthcare_variants)
    ] + [
        _make_company(f"Ad {i}", industry_group=v)
        for i, v in enumerate(adtech_variants)
    ]
    unknown = _make_company("Nicheco", industry_group="diving equipment")
    nullco = _make_company("Nullco", industry_group=None)
    companies += [unknown, nullco]

    db.add_all(companies)
    await db.flush()
    await db.commit()
    ids = [c.id for c in companies]
    unknown_id, null_id = unknown.id, nullco.id

    summary = await run_normalize_taxonomy(db)

    groups = await _industry_groups(db, ids)
    by_value = Counter(v for v in groups.values() if v is not None)

    # All 5 healthcare rows + all 3 ad-tech rows collapsed onto exactly two
    # canonical buckets; the unknown stayed its own value; the NULL stayed NULL.
    assert by_value["healthcare"] == len(healthcare_variants)
    assert by_value["sales & marketing tech"] == len(adtech_variants)
    assert groups[unknown_id] == "diving equipment"
    assert groups[null_id] is None

    # The stage actually rewrote industry_group rows (not just primary_category).
    # 4 of 5 healthcare variants differ from "healthcare" + all 3 ad-tech
    # variants differ from the canonical → 7 distinct values changed, 7 rows.
    assert summary.industry_group.values_changed == 7
    assert summary.industry_group.rows_updated == 7


async def test_second_run_is_a_noop_on_both_columns(db: AsyncSession) -> None:
    """After one canonicalizing pass, a second run changes nothing on either
    taxonomy column (idempotent)."""
    companies = [
        _make_company(
            "Mixedco A",
            industry_group="healthtech",
            primary_category="ad-tech",
        ),
        _make_company(
            "Mixedco B",
            industry_group="advertising technology",
            primary_category="biotech tooling",
        ),
        _make_company(
            "Mixedco C",
            industry_group="diving equipment",  # passthrough, unchanged
            primary_category="developer tools",  # already canonical
        ),
    ]
    db.add_all(companies)
    await db.flush()
    await db.commit()

    first = await run_normalize_taxonomy(db)
    # First pass did real work on the sprawled values.
    assert first.industry_group.rows_updated > 0

    second = await run_normalize_taxonomy(db)
    assert second.industry_group.values_changed == 0
    assert second.industry_group.rows_updated == 0
    assert second.primary_category.values_changed == 0
    assert second.primary_category.rows_updated == 0
    # Roll-up mirrors the per-column zeros.
    assert second.values_changed == 0
    assert second.rows_updated == 0
