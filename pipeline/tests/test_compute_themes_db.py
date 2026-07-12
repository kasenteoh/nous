"""DB-gated tests for migration 0034 + the compute-themes stage.

Covers, against a real Postgres with pgvector (CI: the pgvector/pgvector:pg15
service image; the schema comes from `alembic upgrade head`):

- models/migration consistency: themes + company_themes round-trip through
  the ORM, centroid is vector(384), the pair-unique constraint exists, and
  RLS is enabled with no policies (0027 pattern);
- the full stage flow: cluster → LLM-name → persist (members
  similarity-ordered, counts + funding metrics + prompt_version stamped);
- the TTL gate (skip when themes are fresh; --force bypass);
- slug stability: an unchanged catalog re-clusters to the same slugs with
  ZERO LLM calls (centroid match), converging modulo the metric refresh;
- replace-per-industry: a stale previous theme no cluster matches is deleted;
- null-over-fabricate: an incoherent cluster (LLM returns null) writes no row;
- funding-growth math against real funding_rounds rows;
- excluded companies never becoming members;
- the LLM budget deferring a whole industry rather than half-replacing it.

The clusterer is always the deterministic FakeClusterer (argmax-based — no
scikit-learn needed) and the LLM is a scripted complete_json stand-in.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanyTheme, FundingRound, Theme
from nous.llm.prompts.theme_naming import PROMPT_VERSION, ThemeNaming
from nous.pipeline.compute_themes import run_compute_themes

from .test_compute_themes import FakeClusterer

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

DIM = 384
TODAY = date(2026, 7, 11)  # windows: recent Q1+Q2 2026, prior Q3+Q4 2025


def _vec(axis: int, jitter: float = 0.0, jitter_axis: int = 100) -> list[float]:
    """A basis vector with an optional small off-axis component.

    argmax stays `axis` (jitter < 1), so the FakeClusterer groups all
    vectors sharing an axis while their exact directions still differ —
    which gives distinct member-to-centroid similarities to order by.
    """
    vec = [0.0] * DIM
    vec[axis] = 1.0
    if jitter:
        vec[jitter_axis] = jitter
    return vec


def _company(slug: str, axis: int, jitter: float = 0.0, **overrides: Any) -> Company:
    defaults: dict[str, Any] = {
        "name": f"Co {slug}",
        "slug": slug,
        "normalized_name": f"co {slug}",
        "description_short": f"Short description for {slug}.",
        "industry_group": "DevTools",
        "embedding": _vec(axis, jitter),
    }
    defaults.update(overrides)
    return Company(**defaults)


def _seed_two_cluster_industry(db_add: Callable[[Any], None]) -> list[Company]:
    """8 shown+embedded DevTools companies: 4 on axis 0, 4 on axis 1.

    choose_k(8) == 2, so the FakeClusterer splits them exactly by axis.
    """
    companies = [
        *(_company(f"alpha-{i}", 0, jitter=0.05 * i) for i in range(4)),
        *(_company(f"beta-{i}", 1, jitter=0.05 * i) for i in range(4)),
    ]
    for company in companies:
        db_add(company)
    return companies


class ScriptedNamer:
    """complete_json stand-in: dispatches a ThemeNaming per prompt."""

    def __init__(self, respond: Callable[[str], ThemeNaming]) -> None:
        self.prompts: list[str] = []
        self._respond = respond

    async def __call__(self, prompt: str, schema: type[ThemeNaming]) -> ThemeNaming:
        assert schema is ThemeNaming
        self.prompts.append(prompt)
        return self._respond(prompt)


def _name_by_cluster(prompt: str) -> ThemeNaming:
    if "alpha-" in prompt:
        return ThemeNaming(
            name="Agentic Coding Tools",
            description="Tools that write and review code with AI agents.",
        )
    return ThemeNaming(
        name="Payments Infrastructure",
        description="APIs and platforms for moving money.",
    )


def _patch_llm(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[str], ThemeNaming] = _name_by_cluster,
) -> ScriptedNamer:
    namer = ScriptedNamer(respond)
    monkeypatch.setattr("nous.pipeline.compute_themes.complete_json", namer)
    return namer


# ---------------------------------------------------------------------------
# Migration 0034 <-> models consistency
# ---------------------------------------------------------------------------


async def test_theme_tables_round_trip(db: AsyncSession) -> None:
    company = _company("round-trip", 0)
    theme = Theme(
        slug="round-trip-theme",
        name="Round Trip",
        industry_group="DevTools",
        description="One sentence.",
        centroid=_vec(0),
        company_count=1,
        funding_recent_usd=Decimal("1000000"),
        funding_prior_usd=Decimal("500000"),
        funding_growth=Decimal("1.0000"),
        prompt_version=PROMPT_VERSION,
    )
    db.add_all([company, theme])
    await db.flush()
    db.add(CompanyTheme(theme_id=theme.id, company_id=company.id, similarity=0.97))
    await db.commit()
    # Captured before expire_all: touching an expired instance's attribute
    # would lazy-load synchronously (MissingGreenlet under the async session).
    company_id = company.id
    db.expire_all()

    fetched = (
        await db.execute(select(Theme).where(Theme.slug == "round-trip-theme"))
    ).scalar_one()
    assert fetched.name == "Round Trip"
    assert len(list(fetched.centroid)) == DIM
    assert fetched.funding_growth == Decimal("1.0000")
    assert fetched.created_at is not None
    assert fetched.updated_at is not None

    membership = (
        await db.execute(
            select(CompanyTheme).where(CompanyTheme.theme_id == fetched.id)
        )
    ).scalar_one()
    assert membership.company_id == company_id
    assert abs(membership.similarity - 0.97) < 1e-9


async def test_theme_schema_pins(db: AsyncSession) -> None:
    """centroid is vector(384); the pair-unique exists; RLS is enabled with no
    policies on both tables (0027 pattern)."""
    centroid_type = (
        await db.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = 'themes'::regclass AND a.attname = 'centroid'"
            )
        )
    ).scalar_one()
    assert centroid_type == "vector(384)"

    unique_name = (
        await db.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'company_themes'::regclass AND contype = 'u'"
            )
        )
    ).scalar_one()
    assert unique_name == "uq_company_themes_theme_company"

    for table in ("themes", "company_themes"):
        rls = (
            await db.execute(
                text(
                    "SELECT relrowsecurity FROM pg_class WHERE relname = :t"
                ).bindparams(t=table)
            )
        ).scalar_one()
        assert rls is True
        policies = (
            await db.execute(
                text("SELECT count(*) FROM pg_policies WHERE tablename = :t")
                .bindparams(t=table)
            )
        ).scalar_one()
        assert policies == 0


# ---------------------------------------------------------------------------
# Stage: cluster → name → persist
# ---------------------------------------------------------------------------


async def test_full_run_creates_named_themes_with_members(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_cluster_industry(db.add)
    await db.commit()
    namer = _patch_llm(monkeypatch)

    summary = await run_compute_themes(db, FakeClusterer(), today=TODAY)

    assert summary.industries_seen == 1
    assert summary.industries_processed == 1
    assert summary.clusters_found == 2
    assert summary.themes_created == 2
    assert summary.themes_matched == 0
    assert summary.memberships_written == 8
    assert summary.llm_calls == 2
    assert len(namer.prompts) == 2

    themes = (
        (await db.execute(select(Theme).order_by(Theme.slug))).scalars().all()
    )
    assert [t.slug for t in themes] == [
        "agentic-coding-tools",
        "payments-infrastructure",
    ]
    for theme in themes:
        assert theme.industry_group == "DevTools"
        assert theme.company_count == 4
        assert theme.description is not None
        assert theme.prompt_version == PROMPT_VERSION

        members = (
            (
                await db.execute(
                    select(CompanyTheme)
                    .where(CompanyTheme.theme_id == theme.id)
                    .order_by(CompanyTheme.similarity.desc())
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 4
        sims = [m.similarity for m in members]
        # Similarities are real cosines to the centroid: within (0, 1] and
        # strictly ordered (the jittered vectors all differ).
        assert all(0.0 < s <= 1.0 + 1e-9 for s in sims)
        assert sims == sorted(sims, reverse=True)


async def test_ttl_gate_skips_fresh_themes_and_force_bypasses(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_cluster_industry(db.add)
    # A theme built "just now" (server_default updated_at) trips the gate.
    db.add(
        Theme(
            slug="fresh-theme",
            name="Fresh Theme",
            industry_group="Fintech",
            centroid=_vec(5),
            company_count=3,
        )
    )
    await db.commit()
    namer = _patch_llm(monkeypatch)

    gated = await run_compute_themes(db, FakeClusterer(), today=TODAY)
    assert gated.skipped_ttl is True
    assert gated.industries_seen == 0
    assert namer.prompts == []

    forced = await run_compute_themes(db, FakeClusterer(), force=True, today=TODAY)
    assert forced.skipped_ttl is False
    assert forced.themes_created == 2


async def test_rerun_is_stable_and_llm_free(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged embeddings converge: same clusters → centroid match at
    cosine 1.0 → same slugs, zero LLM calls, no creates/deletes — only the
    metric refresh."""
    _seed_two_cluster_industry(db.add)
    await db.commit()
    _patch_llm(monkeypatch)

    first = await run_compute_themes(db, FakeClusterer(), today=TODAY)
    assert first.themes_created == 2
    slugs_before = set(
        (await db.execute(select(Theme.slug))).scalars().all()
    )

    second_namer = _patch_llm(monkeypatch)
    second = await run_compute_themes(
        db, FakeClusterer(), force=True, today=TODAY
    )
    assert second.themes_matched == 2
    assert second.themes_created == 0
    assert second.themes_deleted == 0
    assert second_namer.prompts == []  # the LLM seam is never touched

    slugs_after = set((await db.execute(select(Theme.slug))).scalars().all())
    assert slugs_after == slugs_before
    memberships = (
        await db.execute(select(func.count()).select_from(CompanyTheme))
    ).scalar_one()
    assert memberships == 8  # replace-style rewrite, no duplicates


async def test_stale_previous_theme_is_replaced(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_cluster_industry(db.add)
    # A previous DevTools theme whose centroid matches nothing current
    # (orthogonal axis) — stale content that must be deleted, memberships
    # cascading with it.
    stale_company = _company("stale-member", 9, industry_group="DevTools")
    stale = Theme(
        slug="stale-theme",
        name="Stale Theme",
        industry_group="DevTools",
        centroid=_vec(300),
        company_count=3,
    )
    db.add(stale_company)
    db.add(stale)
    await db.flush()
    db.add(
        CompanyTheme(theme_id=stale.id, company_id=stale_company.id, similarity=0.5)
    )
    await db.commit()
    _patch_llm(monkeypatch)

    summary = await run_compute_themes(db, FakeClusterer(), force=True, today=TODAY)

    assert summary.themes_deleted == 1
    slugs = set((await db.execute(select(Theme.slug))).scalars().all())
    assert "stale-theme" not in slugs
    orphaned = (
        await db.execute(
            select(func.count())
            .select_from(CompanyTheme)
            .where(CompanyTheme.theme_id == stale.id)
        )
    ).scalar_one()
    assert orphaned == 0


async def test_incoherent_cluster_is_dropped_not_fabricated(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_cluster_industry(db.add)
    await db.commit()

    def respond(prompt: str) -> ThemeNaming:
        if "alpha-" in prompt:
            return ThemeNaming()  # incoherent: null name + null description
        return _name_by_cluster(prompt)

    _patch_llm(monkeypatch, respond)
    summary = await run_compute_themes(db, FakeClusterer(), today=TODAY)

    assert summary.clusters_incoherent_dropped == 1
    assert summary.themes_created == 1
    themes = (await db.execute(select(Theme))).scalars().all()
    assert [t.slug for t in themes] == ["payments-infrastructure"]
    memberships = (
        await db.execute(select(func.count()).select_from(CompanyTheme))
    ).scalar_one()
    assert memberships == 4  # only the named cluster's members


async def test_funding_growth_from_member_rounds(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    companies = _seed_two_cluster_industry(db.add)
    await db.flush()
    alpha0, alpha1 = companies[0], companies[1]
    beta0 = companies[4]
    db.add_all(
        [
            # Alpha theme: recent (Q1+Q2 2026) = 15M, prior (Q3+Q4 2025) = 5M.
            FundingRound(
                company_id=alpha0.id,
                announced_date=date(2026, 3, 1),
                amount_raised=Decimal("10000000"),
            ),
            FundingRound(
                company_id=alpha1.id,
                announced_date=date(2026, 5, 15),
                amount_raised=Decimal("5000000"),
            ),
            FundingRound(
                company_id=alpha0.id,
                announced_date=date(2025, 8, 1),
                amount_raised=Decimal("5000000"),
            ),
            # In-progress quarter: excluded from the windows.
            FundingRound(
                company_id=alpha0.id,
                announced_date=date(2026, 7, 5),
                amount_raised=Decimal("99000000"),
            ),
            # Beta theme: recent only → growth undefined (NULL).
            FundingRound(
                company_id=beta0.id,
                announced_date=date(2026, 2, 1),
                amount_raised=Decimal("2000000"),
            ),
        ]
    )
    await db.commit()
    _patch_llm(monkeypatch)

    await run_compute_themes(db, FakeClusterer(), today=TODAY)

    alpha_theme = (
        await db.execute(select(Theme).where(Theme.slug == "agentic-coding-tools"))
    ).scalar_one()
    assert alpha_theme.funding_recent_usd == Decimal("15000000")
    assert alpha_theme.funding_prior_usd == Decimal("5000000")
    assert alpha_theme.funding_growth == Decimal("2.0000")

    beta_theme = (
        await db.execute(
            select(Theme).where(Theme.slug == "payments-infrastructure")
        )
    ).scalar_one()
    assert beta_theme.funding_recent_usd == Decimal("2000000")
    assert beta_theme.funding_prior_usd == Decimal("0")
    assert beta_theme.funding_growth is None  # zero base — never an infinity


async def test_excluded_companies_never_become_members(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_cluster_industry(db.add)
    excluded = _company(
        "excluded-alpha", 0, exclusion_reason="not_a_startup"
    )
    db.add(excluded)
    await db.commit()
    _patch_llm(monkeypatch)

    summary = await run_compute_themes(db, FakeClusterer(), today=TODAY)

    assert summary.memberships_written == 8  # the excluded row never counted
    member_ids = set(
        (await db.execute(select(CompanyTheme.company_id))).scalars().all()
    )
    assert excluded.id not in member_ids
    for count in (
        (await db.execute(select(Theme.company_count))).scalars().all()
    ):
        assert count == 4


async def test_small_industries_and_small_clusters_are_skipped(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Industry below MIN_EMBEDDED_COMPANIES (8): 3 companies — never seen.
    for i in range(3):
        db.add(_company(f"tiny-{i}", 0, industry_group="Tiny Industry"))
    # DevTools: 8 companies but 6/2 split — the 2-member cluster is dropped
    # (below MIN_CLUSTER_SIZE), only the 6-member cluster is named.
    for i in range(6):
        db.add(_company(f"big-{i}", 0, jitter=0.04 * i))
    for i in range(2):
        db.add(_company(f"small-{i}", 1))
    await db.commit()
    namer = _patch_llm(monkeypatch)

    summary = await run_compute_themes(db, FakeClusterer(), today=TODAY)

    assert summary.industries_seen == 1  # only DevTools qualifies
    assert summary.clusters_small_dropped == 1
    assert summary.clusters_found == 1
    assert summary.themes_created == 1
    assert len(namer.prompts) == 1
    themes = (await db.execute(select(Theme))).scalars().all()
    assert len(themes) == 1
    assert themes[0].company_count == 6


async def test_llm_budget_defers_whole_industry(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 new clusters but budget for 1: the industry is deferred entirely —
    a half-named replace would delete stale themes without successors."""
    _seed_two_cluster_industry(db.add)
    await db.commit()
    namer = _patch_llm(monkeypatch)

    summary = await run_compute_themes(
        db, FakeClusterer(), max_llm_clusters=1, today=TODAY
    )

    assert summary.industries_deferred_cap == 1
    assert summary.themes_created == 0
    assert namer.prompts == []  # deferred BEFORE spending anything
    assert (
        await db.execute(select(func.count()).select_from(Theme))
    ).scalar_one() == 0
