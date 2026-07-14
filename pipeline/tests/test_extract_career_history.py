"""Tests for the extract-career-history stage (dry-run + persisting apply).

Pure unit tests (roster-matching, self-reference / duplicate handling, edge
collapse, yield rendering — no DB, no LLM) plus a DB-gated section that
exercises selection version-gating, the replace-style write, prior_company_id
resolution, and idempotency over real rows (skipped without DATABASE_URL).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nous.db.models import CareerMove, Company, Person, RawPage
from nous.llm.prompts.career_history import (
    PROMPT_VERSION,
    CareerHistoryExtraction,
    PersonCareer,
    PriorRole,
)
from nous.pipeline.extract_career_history import (
    ExtractCareerHistorySummary,
    _compute_company,
    _format_move,
    render_yield_table,
    run_extract_career_history,
)
from nous.util.slugify import normalize_name


def _company(name: str) -> Company:
    return Company(
        name=name,
        slug=f"{normalize_name(name) or 'co'}-{os.urandom(4).hex()}",
        normalized_name=normalize_name(name),
    )


# --------------------------------------------------------------------------- #
# Pure unit tests (no DB, no LLM)
# --------------------------------------------------------------------------- #


def test_format_move_with_and_without_role() -> None:
    assert _format_move("Jane", "Stripe", "Engineer") == "Jane: Engineer @ Stripe"
    assert _format_move("Jane", "Stripe", None) == "Jane: Stripe"


def test_compute_roster_match_and_off_roster() -> None:
    roster = [("Jane Doe", "CEO"), ("John Roe", "CTO")]
    extraction = CareerHistoryExtraction(
        people=[
            PersonCareer(
                name="Jane Doe",
                prior_roles=[
                    PriorRole(company="Stripe", role="Engineer"),
                    PriorRole(company="Google"),
                ],
            ),
            PersonCareer(name="Random Advisor", prior_roles=[PriorRole(company="Sequoia")]),
        ]
    )
    ce = _compute_company(company=_company("Acme"), roster=roster, extraction=extraction)
    assert ce.result.people_on_roster == 1
    assert ce.result.edge_count == 2
    assert ce.result.people_off_roster == 1
    assert ce.result.off_roster_names == ["Random Advisor"]
    assert {m.prior_company_name for m in ce.moves} == {"Stripe", "Google"}
    assert "Jane Doe: Engineer @ Stripe" in ce.result.example_moves


def test_compute_drops_self_reference() -> None:
    roster = [("Jane Doe", "CEO")]
    extraction = CareerHistoryExtraction(
        people=[
            PersonCareer(
                name="Jane Doe",
                prior_roles=[
                    PriorRole(company="Acme", role="CEO"),  # self-reference
                    PriorRole(company="Stripe"),
                ],
            )
        ]
    )
    ce = _compute_company(company=_company("Acme"), roster=roster, extraction=extraction)
    assert ce.result.self_reference_roles == 1
    assert ce.result.edge_count == 1  # only Stripe
    assert [m.prior_company_name for m in ce.moves] == ["Stripe"]


def test_compute_person_with_only_self_reference_is_dropped() -> None:
    roster = [("Jane Doe", "CEO")]
    extraction = CareerHistoryExtraction(
        people=[PersonCareer(name="Jane Doe", prior_roles=[PriorRole(company="Acme")])]
    )
    ce = _compute_company(company=_company("Acme"), roster=roster, extraction=extraction)
    assert ce.result.self_reference_roles == 1
    assert ce.result.people_on_roster == 0
    assert ce.moves == []


def test_compute_merges_duplicate_people() -> None:
    roster = [("Jane Doe", "CEO")]
    extraction = CareerHistoryExtraction(
        people=[
            PersonCareer(name="Jane Doe", prior_roles=[PriorRole(company="Stripe")]),
            PersonCareer(
                name="Jane Doe",
                prior_roles=[PriorRole(company="Stripe"), PriorRole(company="Google")],
            ),
        ]
    )
    ce = _compute_company(company=_company("Widgets"), roster=roster, extraction=extraction)
    assert ce.result.people_on_roster == 1
    assert ce.result.edge_count == 2  # Stripe (deduped) + Google


def test_compute_collapses_two_roles_at_same_prior_company() -> None:
    # Two titles at one employer (a promotion) → one edge (the unique key
    # excludes role); the first-mentioned role wins.
    roster = [("Jane Doe", "CEO")]
    extraction = CareerHistoryExtraction(
        people=[
            PersonCareer(
                name="Jane Doe",
                prior_roles=[
                    PriorRole(company="Google", role="Engineer"),
                    PriorRole(company="Google", role="Director"),
                ],
            )
        ]
    )
    ce = _compute_company(company=_company("Widgets"), roster=roster, extraction=extraction)
    assert ce.result.edge_count == 1
    assert ce.moves[0].prior_company_name == "Google"
    assert ce.moves[0].prior_role == "Engineer"  # first wins


def test_compute_empty_extraction_is_the_common_case() -> None:
    ce = _compute_company(
        company=_company("Husk"),
        roster=[("Jane Doe", "CEO")],
        extraction=CareerHistoryExtraction(),
    )
    assert ce.result.people_on_roster == 0
    assert ce.moves == []


def test_render_yield_table_dry_run_and_apply() -> None:
    dry = ExtractCareerHistorySummary(
        dry_run=True,
        prompt_version=PROMPT_VERSION,
        companies_seen=2,
        companies_with_named_prior=1,
        total_people_with_prior=1,
        total_edges=2,
        example_moves=["Jane Doe: Engineer @ Stripe"],
    )
    md = render_yield_table(dry)
    assert "extract-career-history — dry-run" in md
    assert "Go / no-go" in md
    assert "Jane Doe: Engineer @ Stripe" in md

    apply = dry.model_copy(
        update={"dry_run": False, "rows_written": 2, "companies_stamped": 2}
    )
    md2 = render_yield_table(apply)
    assert "extract-career-history — apply" in md2
    assert "Persisted:" in md2
    assert "Go / no-go" not in md2


# --------------------------------------------------------------------------- #
# DB-gated integration (selection, apply write, resolution, idempotency)
# --------------------------------------------------------------------------- #

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_PAGE_TEXT = (
    "About us. Jane Doe, CEO, was previously an engineer at Stripe and before "
    "that at Google. John Roe is our CTO. " * 4
)


async def _seed(session: AsyncSession, name: str) -> UUID:
    co = _company(name)
    co.latest_round_amount = 1_000_000
    session.add(co)
    await session.flush()
    session.add(RawPage(company_id=co.id, url=f"https://{co.slug}.example/", content=_PAGE_TEXT))
    session.add(Person(company_id=co.id, name="Jane Doe", role="CEO", rank=0))
    session.add(Person(company_id=co.id, name="John Roe", role="CTO", rank=1))
    await session.flush()
    return co.id


def _extraction() -> CareerHistoryExtraction:
    return CareerHistoryExtraction(
        people=[
            PersonCareer(
                name="Jane Doe",
                prior_roles=[
                    PriorRole(company="Stripe", role="Engineer"),
                    PriorRole(company="Google"),
                ],
            )
        ]
    )


@_DB
async def test_dry_run_writes_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = await _seed(db, "Dry Co")
    monkeypatch.setattr(
        "nous.pipeline.extract_career_history.complete_json",
        AsyncMock(return_value=_extraction()),
    )
    summary = await run_extract_career_history(db, limit=10, dry_run=True)
    assert summary.companies_with_named_prior >= 1
    assert summary.rows_written == 0
    rows = (
        await db.execute(select(CareerMove.id).where(CareerMove.company_id == cid))
    ).all()
    assert rows == []  # nothing persisted
    stamp = (
        await db.execute(
            select(Company.career_extracted_prompt_version).where(Company.id == cid)
        )
    ).scalar_one()
    assert stamp is None  # not stamped in dry-run


@_DB
async def test_apply_persists_resolves_and_is_idempotent(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = await _seed(db, "Apply Co")
    # A catalog company named "Stripe" so the Stripe edge resolves prior_company_id.
    stripe = _company("Stripe")
    db.add(stripe)
    await db.flush()
    monkeypatch.setattr(
        "nous.pipeline.extract_career_history.complete_json",
        AsyncMock(return_value=_extraction()),
    )

    summary = await run_extract_career_history(db, limit=10, dry_run=False)
    assert summary.rows_written == 2
    assert summary.prior_company_ids_resolved == 1  # only Stripe is in-catalog
    assert summary.companies_stamped >= 1

    moves = (
        (await db.execute(select(CareerMove).where(CareerMove.company_id == cid)))
        .scalars()
        .all()
    )
    assert {m.prior_company_name for m in moves} == {"Stripe", "Google"}
    by_name = {m.prior_company_name: m for m in moves}
    assert by_name["Stripe"].prior_company_id == stripe.id  # resolved
    assert by_name["Google"].prior_company_id is None  # not in catalog
    assert all(m.extraction_prompt_version == PROMPT_VERSION for m in moves)

    stamp = (
        await db.execute(
            select(Company.career_extracted_prompt_version).where(Company.id == cid)
        )
    ).scalar_one()
    assert stamp == PROMPT_VERSION

    # Idempotent: the stamp means a second run at the same version does not
    # re-select the company (no re-extraction, no re-billing) — the point of the
    # per-company stamp.
    mock2 = AsyncMock(return_value=_extraction())
    monkeypatch.setattr("nous.pipeline.extract_career_history.complete_json", mock2)
    summary2 = await run_extract_career_history(db, limit=10, dry_run=False)
    assert all(r.name != "Apply Co" for r in summary2.results)
    assert mock2.await_count == summary2.companies_seen


@_DB
async def test_apply_survives_per_company_rollback(
    committed_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression (review PR-C, high): a per-company error triggers
    # session.rollback(), which expires the WHOLE identity map; the loop must
    # still process later companies. Driving off ids + re-get (not preloaded ORM
    # objects) is what prevents a MissingGreenlet crash on the next iteration.
    async with committed_session_factory() as s1:
        await _seed(s1, "AAA Rollback Co")
        await _seed(s1, "BBB Rollback Co")
        await s1.commit()

    monkeypatch.setattr(
        "nous.pipeline.extract_career_history.complete_json",
        AsyncMock(return_value=_extraction()),
    )
    from nous.pipeline import extract_career_history as mod

    real_persist = mod._persist_company
    state = {"n": 0}

    async def flaky(session, *, company, moves, resolve_cache):  # type: ignore[no-untyped-def]
        state["n"] += 1
        if state["n"] == 1:  # first company: force the rollback path
            raise IntegrityError("boom", None, Exception("boom"))
        return await real_persist(
            session, company=company, moves=moves, resolve_cache=resolve_cache
        )

    monkeypatch.setattr(mod, "_persist_company", flaky)

    async with committed_session_factory() as s2:
        summary = await run_extract_career_history(s2, limit=10, dry_run=False)

    assert summary.companies_seen == 2  # BOTH processed — no crash
    assert summary.errors == 1  # the first raised
    assert summary.companies_stamped == 1  # the second still succeeded


@_DB
async def test_apply_empty_extraction_stamps_without_rows(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ~85% case: no named pedigree → zero rows, but the company is STILL
    # stamped so it is not re-extracted next run.
    cid = await _seed(db, "Empty Co")
    monkeypatch.setattr(
        "nous.pipeline.extract_career_history.complete_json",
        AsyncMock(return_value=CareerHistoryExtraction()),
    )
    summary = await run_extract_career_history(db, limit=10, dry_run=False)
    assert summary.rows_written == 0
    rows = (
        await db.execute(select(CareerMove.id).where(CareerMove.company_id == cid))
    ).all()
    assert rows == []
    stamp = (
        await db.execute(
            select(Company.career_extracted_prompt_version).where(Company.id == cid)
        )
    ).scalar_one()
    assert stamp == PROMPT_VERSION  # stamped despite zero rows
