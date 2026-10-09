"""Unit tests for purge-wrong-entity-batch — the retroactive entity-audit queue.

The probe and the per-company purge are faked at the module seam (both have
their own DB-gated suites), so these run everywhere: queue order/collapse,
paging, outcome classification (purged / clean / held / skipped), stop on a
rate limit, the runtime budget, and that dry-run is forwarded untouched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

import nous.pipeline.purge_wrong_entity_batch as batch
from nous.pipeline.audit_round_entities import AuditRoundEntitiesSummary, SuspectRound
from nous.pipeline.purge_wrong_entity_articles import (
    ArticleVerdict,
    PurgeRateLimitedError,
    PurgeWrongEntityError,
    PurgeWrongEntitySummary,
)


def _suspect(slug: str, amount: str | None) -> SuspectRound:
    return SuspectRound(
        slug=slug, amount=amount, text_kind="body", reasons=["x"], round_id="r"
    )


def test_build_queue_collapses_and_keeps_largest_first() -> None:
    queue = batch.build_queue(
        [
            ("built", "$30,000,000,000"),
            ("blue", "$10,000,000,000"),
            ("built", "$5,000,000"),
            ("magic", "$500,000,000"),
            ("magic", "$300,000,000"),
            ("magic", None),
        ]
    )
    assert queue == [
        ("built", "$30,000,000,000", 2),
        ("blue", "$10,000,000,000", 1),
        ("magic", "$500,000,000", 3),
    ]


class _FakeSessionFactory:
    def __call__(self) -> Any:
        @asynccontextmanager
        async def _cm() -> AsyncIterator[None]:
            yield None

        return _cm()


def _purge_result(
    slug: str, *, checked: int, purged: int, held: bool = False
) -> PurgeWrongEntitySummary:
    verdicts = [
        ArticleVerdict(title=f"{slug} other {i}", url=f"u{i}", keep=False,
                       reason="adjudicated", other_entity="Other Co")
        for i in range(purged)
    ] + [
        ArticleVerdict(title=f"{slug} ours {i}", url=f"k{i}", keep=True, reason="ok")
        for i in range(checked - purged)
    ]
    return PurgeWrongEntitySummary(
        slug=slug,
        articles_checked=checked,
        articles_purged=purged,
        articles_kept=checked - purged,
        rounds_purged=1 if purged and not held else 0,
        round_labels=["Series B: $10 (—)"] if purged and not held else [],
        held=held,
        hold_reason="mostly another entity" if held else None,
        verdicts=verdicts,
    )


@pytest.fixture
def fake_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"purged": [], "dry_run": [], "audit_limit": "unset"}
    suspects = [
        _suspect("blue", "$10,000,000,000"),
        _suspect("wave", "$2,000,000,000"),
        _suspect("blue", "$1,000"),
        _suspect("nodesc", "$900,000,000"),
        _suspect("clean-co", "$800,000,000"),
        _suspect("late", "$1,000,000"),
    ]

    async def _audit(session: Any, *, min_amount: Any, suspect_limit: Any) -> Any:
        calls["audit_limit"] = suspect_limit
        return AuditRoundEntitiesSummary(suspects=suspects)

    plans: dict[str, Any] = {
        "blue": _purge_result("blue", checked=10, purged=10, held=True),
        "wave": _purge_result("wave", checked=8, purged=3),
        "nodesc": PurgeWrongEntityError("'nodesc' has no description"),
        "clean-co": _purge_result("clean-co", checked=5, purged=0),
        "late": _purge_result("late", checked=2, purged=1),
    }

    async def _purge(session: Any, *, slug: str, dry_run: bool, **kw: Any) -> Any:
        calls["purged"].append(slug)
        calls["dry_run"].append(dry_run)
        plan = plans[slug]
        if isinstance(plan, Exception):
            raise plan
        return plan

    monkeypatch.setattr(batch, "run_audit_round_entities", _audit)
    monkeypatch.setattr(batch, "run_purge_wrong_entity_articles", _purge)
    calls["plans"] = plans
    return calls


async def test_batch_classifies_every_outcome(fake_seams: dict[str, Any]) -> None:
    summary = await batch.run_purge_wrong_entity_batch(
        _FakeSessionFactory(), limit=10  # type: ignore[arg-type]
    )
    assert fake_seams["audit_limit"] is None  # uncapped suspect list
    assert fake_seams["purged"] == ["blue", "wave", "nodesc", "clean-co", "late"]
    assert all(fake_seams["dry_run"])  # dry-run by default, forwarded
    assert summary.dry_run is True
    assert summary.suspect_rounds_total == 6
    assert summary.companies_in_queue == 5

    by_slug = {r.slug: r for r in summary.results}
    assert by_slug["blue"].outcome == "held"
    assert by_slug["blue"].suspect_rounds == 2
    assert by_slug["wave"].outcome == "would_purge"
    assert by_slug["wave"].purged_titles == [
        f"wave other {i} — Other Co" for i in range(3)
    ]
    assert by_slug["nodesc"].outcome == "skipped"
    assert by_slug["nodesc"].note and "no description" in by_slug["nodesc"].note
    assert by_slug["clean-co"].outcome == "clean"

    # Held companies' verdicts never count toward the purge totals.
    assert summary.companies_held == 1
    assert summary.companies_purged == 2
    assert summary.companies_clean == 1
    assert summary.companies_skipped == 1
    assert summary.articles_purged == 3 + 1
    assert summary.rounds_purged == 2
    assert summary.articles_checked == 10 + 8 + 5 + 2


async def test_batch_apply_labels_purged(fake_seams: dict[str, Any]) -> None:
    summary = await batch.run_purge_wrong_entity_batch(
        _FakeSessionFactory(), limit=2, offset=1, dry_run=False  # type: ignore[arg-type]
    )
    assert fake_seams["purged"] == ["wave", "nodesc"]
    assert fake_seams["dry_run"] == [False, False]
    assert [r.outcome for r in summary.results] == ["purged", "skipped"]
    assert summary.offset == 1


async def test_batch_stops_whole_queue_on_rate_limit(
    fake_seams: dict[str, Any],
) -> None:
    fake_seams["plans"]["wave"] = PurgeRateLimitedError("429")
    summary = await batch.run_purge_wrong_entity_batch(
        _FakeSessionFactory(), limit=10  # type: ignore[arg-type]
    )
    assert fake_seams["purged"] == ["blue", "wave"]  # nothing after the 429
    assert summary.aborted_rate_limited is True
    assert summary.results[-1].outcome == "skipped"


async def test_batch_runtime_budget_stops_at_company_boundary(
    fake_seams: dict[str, Any],
) -> None:
    summary = await batch.run_purge_wrong_entity_batch(
        _FakeSessionFactory(), limit=10, max_runtime_minutes=0  # type: ignore[arg-type]
    )
    assert fake_seams["purged"] == []
    assert summary.stopped_early is True


async def test_render_table_mentions_every_company(fake_seams: dict[str, Any]) -> None:
    summary = await batch.run_purge_wrong_entity_batch(
        _FakeSessionFactory(), limit=10  # type: ignore[arg-type]
    )
    table = batch.render_batch_table(summary)
    assert "DRY-RUN" in table
    for slug in ("blue", "wave", "nodesc", "clean-co", "late"):
        assert f"`{slug}`" in table


async def test_batch_operator_skip_keeps_queue_position(
    fake_seams: dict[str, Any],
) -> None:
    summary = await batch.run_purge_wrong_entity_batch(
        _FakeSessionFactory(),  # type: ignore[arg-type]
        limit=3,
        skip=frozenset({"wave"}),
    )
    assert fake_seams["purged"] == ["blue", "nodesc"]  # wave never adjudicated
    assert [r.outcome for r in summary.results] == ["held", "operator_skip", "skipped"]
    assert summary.companies_operator_skipped == 1
