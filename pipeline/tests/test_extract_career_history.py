"""Tests for the extract-career-history dry-run stage.

Split into pure unit tests (roster-matching, yield rendering, the apply-path
guard — no DB, no LLM) and a DB-gated section that exercises selection + the
patched LLM call over real rows (skipped without DATABASE_URL, same as the other
stage suites; runs in CI's Postgres service).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Person, RawPage
from nous.llm.prompts.career_history import (
    CareerHistoryExtraction,
    PersonCareer,
    PriorRole,
)
from nous.pipeline.extract_career_history import (
    ExtractCareerHistorySummary,
    _format_move,
    _summarize_company,
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


def test_summarize_company_roster_match_and_off_roster() -> None:
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
            # Off-roster: the prompt forbids it — counted as a fabrication proxy.
            PersonCareer(name="Random Advisor", prior_roles=[PriorRole(company="Sequoia")]),
        ]
    )
    result = _summarize_company(
        company=_company("Acme"), roster=roster, extraction=extraction
    )
    assert result.people_on_roster == 1
    assert result.prior_role_count == 2
    assert result.people_off_roster == 1
    assert result.off_roster_names == ["Random Advisor"]
    assert "Jane Doe: Engineer @ Stripe" in result.example_moves


def test_summarize_company_matches_stylized_names() -> None:
    # normalize_name collapses styling so "Open AI" roster still matches "OpenAI".
    roster = [("Sam Altman", "CEO")]
    extraction = CareerHistoryExtraction(
        people=[PersonCareer(name="  Sam   Altman ", prior_roles=[PriorRole(company="YC")])]
    )
    result = _summarize_company(
        company=_company("X"), roster=roster, extraction=extraction
    )
    assert result.people_on_roster == 1
    assert result.people_off_roster == 0


def test_summarize_company_drops_self_reference() -> None:
    # The model sometimes echoes the CURRENT company as a "prior" employer; that
    # must not inflate the yield — it's dropped and counted as a proxy.
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
    result = _summarize_company(
        company=_company("Acme"), roster=roster, extraction=extraction
    )
    assert result.self_reference_roles == 1
    assert result.prior_role_count == 1  # only Stripe survives
    assert result.people_on_roster == 1
    assert result.example_moves == ["Jane Doe: Stripe"]


def test_summarize_company_person_with_only_self_reference_is_dropped() -> None:
    roster = [("Jane Doe", "CEO")]
    extraction = CareerHistoryExtraction(
        people=[PersonCareer(name="Jane Doe", prior_roles=[PriorRole(company="Acme")])]
    )
    result = _summarize_company(
        company=_company("Acme"), roster=roster, extraction=extraction
    )
    assert result.self_reference_roles == 1
    assert result.people_on_roster == 0  # no real prior → not counted
    assert result.prior_role_count == 0


def test_summarize_company_merges_duplicate_people() -> None:
    # A founder emitted twice (or split) must count once, roles de-duplicated.
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
    result = _summarize_company(
        company=_company("Widgets"), roster=roster, extraction=extraction
    )
    assert result.people_on_roster == 1  # merged, not double-counted
    assert result.prior_role_count == 2  # Stripe (deduped) + Google


def test_summarize_company_empty_extraction_is_the_common_case() -> None:
    result = _summarize_company(
        company=_company("Husk"),
        roster=[("Jane Doe", "CEO")],
        extraction=CareerHistoryExtraction(),
    )
    assert result.people_on_roster == 0
    assert result.prior_role_count == 0
    assert result.people_off_roster == 0


def test_render_yield_table_smoke() -> None:
    summary = ExtractCareerHistorySummary(
        dry_run=True,
        prompt_version="2026-07-13.1",
        companies_seen=2,
        companies_with_named_prior=1,
        total_people_with_prior=1,
        total_prior_roles=2,
        total_off_roster_people=0,
        example_moves=["Jane Doe: Engineer @ Stripe"],
    )
    md = render_yield_table(summary)
    assert "extract-career-history — dry-run yield" in md
    assert "2026-07-13.1" in md
    assert "Jane Doe: Engineer @ Stripe" in md
    assert "Go / no-go" in md


async def test_apply_path_not_implemented() -> None:
    # dry_run=False raises before touching the session (persistence lands with
    # migration 0040), so passing no real session is fine.
    with pytest.raises(NotImplementedError):
        await run_extract_career_history(None, dry_run=False)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# DB-gated integration (selection + patched LLM over real rows)
# --------------------------------------------------------------------------- #

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_PAGE_TEXT = (
    "About us. Jane Doe, CEO, was previously an engineer at Stripe and before "
    "that at Google. John Roe is our CTO. " * 4
)


async def _seed_company_with_roster(session: AsyncSession, name: str) -> UUID:
    co = _company(name)
    co.latest_round_amount = 1_000_000  # prominence for deterministic ordering
    session.add(co)
    await session.flush()
    session.add(RawPage(company_id=co.id, url=f"https://{co.slug}.example/", content=_PAGE_TEXT))
    session.add(Person(company_id=co.id, name="Jane Doe", role="CEO", rank=0))
    session.add(Person(company_id=co.id, name="John Roe", role="CTO", rank=1))
    await session.flush()
    return co.id


@_DB
async def test_run_extracts_and_tallies_roster_match(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_company_with_roster(db, "Extract Me")

    mock = AsyncMock(
        return_value=CareerHistoryExtraction(
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
    )
    monkeypatch.setattr("nous.pipeline.extract_career_history.complete_json", mock)

    summary = await run_extract_career_history(db, limit=10, dry_run=True)

    assert summary.companies_seen >= 1
    assert summary.companies_with_named_prior >= 1
    assert summary.total_prior_roles >= 2
    assert summary.total_off_roster_people == 0
    assert mock.await_count == summary.companies_seen  # one call per company


@_DB
async def test_run_empty_extraction_yields_no_named_prior(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ~85% case: the bio names no pedigree → empty extraction, zero yield.
    await _seed_company_with_roster(db, "No Pedigree Co")
    monkeypatch.setattr(
        "nous.pipeline.extract_career_history.complete_json",
        AsyncMock(return_value=CareerHistoryExtraction()),
    )

    summary = await run_extract_career_history(db, limit=10, dry_run=True)

    assert summary.companies_seen >= 1
    assert summary.total_people_with_prior == 0
    assert summary.total_prior_roles == 0
