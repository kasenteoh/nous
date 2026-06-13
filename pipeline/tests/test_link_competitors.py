"""Integration tests for the link-competitors pipeline stage.

Requires DATABASE_URL pointing at a live Postgres with the schema applied
(``alembic upgrade head``) and the pg_trgm extension installed — the stage
relies on ``similarity()`` over ``companies.normalized_name``. Skipped when
DATABASE_URL is unset.

The ``db`` fixture (conftest.py) yields a savepoint-isolated AsyncSession whose
outer transaction is rolled back at teardown, so committed work inside the
stage's per-row loop is undone between tests. Distinct ``linkcomp-`` slug
prefixes keep fixtures from colliding.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Competitor
from nous.pipeline.link_competitors import run_link_competitors
from nous.util.slugify import normalize_name

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(*, name: str, slug: str, normalized_name: str) -> Company:
    """A minimal company row. ``normalized_name`` is set explicitly so tests
    control the trigram-match target precisely (rather than relying on a
    slug-derived value)."""
    return Company(
        name=name,
        slug=slug,
        normalized_name=normalized_name,
        hq_country="US",
    )


def _make_competitor(
    *,
    company_id: object,
    competitor_name: str,
    rank: int = 1,
    competitor_company_id: object | None = None,
) -> Competitor:
    return Competitor(
        company_id=company_id,
        competitor_company_id=competitor_company_id,
        competitor_name=competitor_name,
        rank=rank,
        source="llm_inferred",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_single_clear_match_is_linked(db: AsyncSession) -> None:
    """A dangling competitor that fuzzy-matches exactly ONE company above
    threshold gets its FK set to that company."""
    subject = _make_company(
        name="Subject Alpha Inc.",
        slug="linkcomp-link-subject",
        normalized_name="subjectalpha",
    )
    target = _make_company(
        name="Globex Analytics Inc.",
        slug="linkcomp-link-target",
        normalized_name="globexanalytics",
    )
    db.add_all([subject, target])
    await db.flush()

    comp = _make_competitor(
        company_id=subject.id,
        competitor_name="Globex Analytics",  # normalizes to "globexanalytics"
        competitor_company_id=None,
    )
    db.add(comp)
    await db.flush()
    await db.commit()

    summary = await run_link_competitors(db)

    assert summary.linked >= 1
    await db.refresh(comp)
    assert comp.competitor_company_id == target.id


async def test_ambiguous_match_is_left_null(db: AsyncSession) -> None:
    """Two companies with near-equal high similarity leave the FK NULL
    (skipped_ambiguous) — the stage refuses to guess."""
    subject = _make_company(
        name="Ambig Subject Inc.",
        slug="linkcomp-amb-subject",
        normalized_name="ambigsubject",
    )
    # "initech" scores 0.583 against BOTH of these — a zero margin, well within
    # the 0.08 tie_margin — so neither can be chosen.
    target_one = _make_company(
        name="Initech One Inc.",
        slug="linkcomp-amb-one",
        normalized_name="initechone",
    )
    target_two = _make_company(
        name="Initech Two Inc.",
        slug="linkcomp-amb-two",
        normalized_name="initechtwo",
    )
    db.add_all([subject, target_one, target_two])
    await db.flush()

    comp = _make_competitor(
        company_id=subject.id,
        competitor_name="Initech",  # normalizes to "initech"
        competitor_company_id=None,
    )
    db.add(comp)
    await db.flush()
    await db.commit()

    summary = await run_link_competitors(db)

    assert summary.skipped_ambiguous >= 1
    assert summary.linked == 0
    await db.refresh(comp)
    assert comp.competitor_company_id is None


async def test_self_match_is_left_null(db: AsyncSession) -> None:
    """When the best match is the subject company itself (no better other
    candidate), the FK is left NULL (skipped_self)."""
    subject = _make_company(
        name="Hooli Inc.",
        slug="linkcomp-self-subject",
        normalized_name="hooli",
    )
    # A far-away company exists but scores 0.0 — so the subject itself is the
    # single best (and only) candidate above threshold.
    other = _make_company(
        name="Faraway Unrelated Inc.",
        slug="linkcomp-self-other",
        normalized_name="farawayunrelated",
    )
    db.add_all([subject, other])
    await db.flush()

    comp = _make_competitor(
        company_id=subject.id,
        competitor_name="Hooli",  # normalizes to "hooli" == subject
        competitor_company_id=None,
    )
    db.add(comp)
    await db.flush()
    await db.commit()

    summary = await run_link_competitors(db)

    assert summary.skipped_self >= 1
    assert summary.linked == 0
    await db.refresh(comp)
    assert comp.competitor_company_id is None


async def test_already_resolved_row_is_untouched(db: AsyncSession) -> None:
    """A competitor whose FK is already set is never selected or changed."""
    subject = _make_company(
        name="Resolved Subject Inc.",
        slug="linkcomp-resolved-subject",
        normalized_name="resolvedsubject",
    )
    target = _make_company(
        name="Globex Analytics Inc.",
        slug="linkcomp-resolved-target",
        normalized_name="globexanalytics",
    )
    # A second strong match exists; if the stage *re-evaluated* resolved rows it
    # might flip the FK or trip the tie guard. It must do neither.
    decoy = _make_company(
        name="Globex Analytica Inc.",
        slug="linkcomp-resolved-decoy",
        normalized_name="globexanalytica",
    )
    db.add_all([subject, target, decoy])
    await db.flush()

    comp = _make_competitor(
        company_id=subject.id,
        competitor_name="Globex Analytics",
        competitor_company_id=target.id,  # already resolved
    )
    db.add(comp)
    await db.flush()
    await db.commit()

    summary = await run_link_competitors(db)

    assert summary.rows_seen == 0  # NULL-FK-only selection excludes it
    await db.refresh(comp)
    assert comp.competitor_company_id == target.id


async def test_dry_run_performs_no_writes(db: AsyncSession) -> None:
    """dry_run=True reports counts but leaves the FK NULL."""
    subject = _make_company(
        name="Dry Subject Inc.",
        slug="linkcomp-dry-subject",
        normalized_name="drysubject",
    )
    target = _make_company(
        name="Globex Analytics Inc.",
        slug="linkcomp-dry-target",
        normalized_name="globexanalytics",
    )
    db.add_all([subject, target])
    await db.flush()

    comp = _make_competitor(
        company_id=subject.id,
        competitor_name="Globex Analytics",
        competitor_company_id=None,
    )
    db.add(comp)
    await db.flush()
    await db.commit()

    summary = await run_link_competitors(db, dry_run=True)

    assert summary.linked >= 1  # it *would* have linked
    await db.refresh(comp)
    assert comp.competitor_company_id is None  # but wrote nothing


async def test_rerun_is_noop(db: AsyncSession) -> None:
    """A second run after a successful link is a no-op: the now-resolved row is
    no longer selected, so rows_seen drops."""
    subject = _make_company(
        name="Idem Subject Inc.",
        slug="linkcomp-idem-subject",
        normalized_name="idemsubject",
    )
    target = _make_company(
        name="Globex Analytics Inc.",
        slug="linkcomp-idem-target",
        normalized_name="globexanalytics",
    )
    db.add_all([subject, target])
    await db.flush()

    comp = _make_competitor(
        company_id=subject.id,
        competitor_name="Globex Analytics",
        competitor_company_id=None,
    )
    db.add(comp)
    await db.flush()
    await db.commit()

    first = await run_link_competitors(db)
    assert first.linked >= 1
    await db.refresh(comp)
    assert comp.competitor_company_id == target.id

    second = await run_link_competitors(db)
    # The linked row is no longer NULL, so it isn't picked up again.
    assert second.rows_seen == 0
    assert second.linked == 0


async def test_no_candidate_above_threshold_is_no_match(db: AsyncSession) -> None:
    """A competitor name with nothing similar enough is skipped_no_match and
    left NULL."""
    subject = _make_company(
        name="Lonely Subject Inc.",
        slug="linkcomp-nomatch-subject",
        normalized_name="lonelysubject",
    )
    db.add(subject)
    await db.flush()

    comp = _make_competitor(
        company_id=subject.id,
        # Nothing in the DB is trigram-close to this.
        competitor_name="Zzqx Quux Widgets",
        competitor_company_id=None,
    )
    db.add(comp)
    await db.flush()
    await db.commit()

    summary = await run_link_competitors(db)

    assert summary.skipped_no_match >= 1
    assert summary.linked == 0
    await db.refresh(comp)
    assert comp.competitor_company_id is None


async def test_limit_caps_rows_processed(db: AsyncSession) -> None:
    """limit caps how many dangling rows the stage selects."""
    subject = _make_company(
        name="Limit Subject Inc.",
        slug="linkcomp-limit-subject",
        normalized_name="limitsubject",
    )
    db.add(subject)
    await db.flush()

    for i in range(3):
        db.add(
            _make_competitor(
                company_id=subject.id,
                competitor_name=f"Nomatch Token {i}",
                rank=i + 1,
                competitor_company_id=None,
            )
        )
    await db.flush()
    await db.commit()

    summary = await run_link_competitors(db, limit=1)
    assert summary.rows_seen == 1


def test_normalize_name_sanity() -> None:
    """Guard the fixture assumptions: the competitor names normalize to the
    tokens the trigram targets are built around."""
    assert normalize_name("Globex Analytics") == "globexanalytics"
    assert normalize_name("Initech") == "initech"
    assert normalize_name("Hooli") == "hooli"


async def test_match_below_threshold_via_low_threshold_links(
    db: AsyncSession,
) -> None:
    """The threshold knob is honored: a pair too weak at the default 0.45 links
    once threshold is lowered (and only one candidate exists, so no tie)."""
    subject = _make_company(
        name="Thresh Subject Inc.",
        slug="linkcomp-thresh-subject",
        normalized_name="threshsubject",
    )
    # "stripepayments" vs "stripe" ~ 0.375: below 0.45, above 0.30.
    target = _make_company(
        name="Stripe Inc.",
        slug="linkcomp-thresh-target",
        normalized_name="stripe",
    )
    db.add_all([subject, target])
    await db.flush()

    comp = _make_competitor(
        company_id=subject.id,
        competitor_name="Stripe Payments",  # normalizes to "stripepayments"
        competitor_company_id=None,
    )
    db.add(comp)
    await db.flush()
    await db.commit()

    # Default threshold: no match.
    default_summary = await run_link_competitors(db)
    assert default_summary.skipped_no_match >= 1
    await db.refresh(comp)
    assert comp.competitor_company_id is None

    # Lowered threshold: links.
    low_summary = await run_link_competitors(db, threshold=0.3)
    assert low_summary.linked >= 1
    await db.refresh(comp)
    assert comp.competitor_company_id == target.id


async def test_stmt_returns_competitor_rows(db: AsyncSession) -> None:
    """Sanity: after a link, the resolved FK is queryable as a real edge (the
    point of the densification)."""
    subject = _make_company(
        name="Edge Subject Inc.",
        slug="linkcomp-edge-subject",
        normalized_name="edgesubject",
    )
    target = _make_company(
        name="Globex Analytics Inc.",
        slug="linkcomp-edge-target",
        normalized_name="globexanalytics",
    )
    db.add_all([subject, target])
    await db.flush()

    comp = _make_competitor(
        company_id=subject.id,
        competitor_name="Globex Analytics",
        competitor_company_id=None,
    )
    db.add(comp)
    await db.flush()
    await db.commit()

    await run_link_competitors(db)

    rows = (
        (
            await db.execute(
                select(Competitor).where(
                    Competitor.competitor_company_id == target.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].company_id == subject.id
