"""Integration tests for the judge-eligibility backfill stage.

``complete_json`` is monkeypatched; requires DATABASE_URL (same gating as the
other DB suites).

``run_judge_eligibility`` takes a SESSION FACTORY (not one shared session) and
processes each company in its own short-lived session, with the DB operations
bounded by a per-op timeout. That way one wedged free-tier connection skips a
single company instead of stalling the whole stage (the 2026-06-13 hang). These
tests use ``committed_session_factory`` (conftest) so the stage's per-company
sessions and the verification reads run as the CLI does: separate sessions over
one isolated connection.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nous.db.models import Company, FundingRound, RawPage
from nous.llm.client import LLMRateLimitError
from nous.llm.prompts.company_eligibility import EligibilityJudgment
from nous.pipeline.judge_eligibility import (
    nonstartup_signal_clause,
    run_judge_eligibility,
)

# ---------------------------------------------------------------------------
# Pure-unit tests for nonstartup_signal_clause — no DATABASE_URL required.
# Compile the predicate to SQL and assert it covers BOTH description columns
# and the headline non-startup signals (the Manta / Lucra leak). These run in
# CI without Postgres; they are defined ABOVE the DB ``pytestmark`` below.
# ---------------------------------------------------------------------------


def _compiled_clause_sql() -> str:
    compiled = nonstartup_signal_clause().compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled).lower()


def test_signal_clause_covers_both_description_columns() -> None:
    sql = _compiled_clause_sql()
    assert "description_short" in sql
    assert "description_long" in sql
    assert " ilike " in sql  # case-insensitive matching


def test_signal_clause_matches_directory_and_coaching_phrases() -> None:
    """The two live leaks (Manta = a directory, Lucra = coaching) plus the other
    rejected categories must each be representable in the predicate."""
    sql = _compiled_clause_sql()
    assert "business directory" in sql  # Manta
    assert "online directory" in sql
    assert "courses, coaching" in sql  # Lucra
    assert "mindset" in sql
    assert "marketing agency" in sql
    assert "consultancy" in sql
    # Decades-old wording (Manta: "operating for over 20 years").
    assert "for over 20 years" in sql


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

# NB: the module-level ``pytestmark`` above gates every test DEFINED BELOW this
# line on DATABASE_URL; the pure-unit predicate tests above it always run.

Factory = async_sessionmaker[AsyncSession]


def _enriched_company(name: str, slug_prefix: str) -> Company:
    # A random slug suffix keeps fixtures from colliding on the unique slug:
    # committed_session_factory commits into a shared outer transaction, so a
    # crashed prior run could otherwise leave a fixed slug behind.
    return Company(
        name=name,
        slug=f"{slug_prefix}-{os.urandom(4).hex()}",
        normalized_name=name.lower(),
        hq_country="US",
        description_short="Does things.",
        description_long="Does many things.",
        last_enriched_at=datetime.now(tz=UTC),
    )


async def test_judgment_excludes_and_stamps(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with committed_session_factory() as s1:
        old_co = _enriched_company("Old Enterprise", "old-enterprise-judge")
        s1.add(old_co)
        await s1.flush()
        old_id: UUID = old_co.id
        s1.add(
            RawPage(
                company_id=old_id,
                url="https://old.example/",
                content="Serving the enterprise since 2000." * 20,
            )
        )
        await s1.commit()

    canned = EligibilityJudgment(
        is_startup=False,
        not_startup_reason="Founded in 2000; publicly traded.",
        founded_year=2000,
    )
    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_judge_eligibility(committed_session_factory)
    assert summary.companies_judged == 1
    assert summary.companies_excluded == 1

    # The write must be visible from a SEPARATE session (it commits, not flushes).
    async with committed_session_factory() as s3:
        refetched = await s3.get(Company, old_id)
    assert refetched is not None
    assert refetched.exclusion_reason == "not_a_startup"
    assert refetched.eligibility_checked_at is not None
    assert refetched.year_incorporated == 2000

    # Second run selects nothing — the stamp makes the backfill one-shot.
    summary2 = await run_judge_eligibility(committed_session_factory)
    assert summary2.companies_judged == 0


async def test_prominence_override_keeps_mega_raiser_but_excludes_the_rest(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner override (2026-07-20): a not-a-startup verdict is overturned for a
    company with a >= $500M recorded round (the blue-origin case) — it stays
    SHOWN and is stamped like a kept row — while a non-prominent company with the
    SAME verdict is still excluded and stamped. One run, one mocked verdict."""
    async with committed_session_factory() as s1:
        mega = _enriched_company("Aaa Mega Raiser", "prom-judge-mega")
        small = _enriched_company("Bbb Small Co", "prom-judge-small")
        s1.add_all([mega, small])
        await s1.flush()
        mega_id: UUID = mega.id
        small_id: UUID = small.id
        s1.add_all(
            [
                RawPage(
                    company_id=mega_id,
                    url="https://mega.example/",
                    content="Building rockets for two decades." * 20,
                ),
                RawPage(
                    company_id=small_id,
                    url="https://small.example/",
                    content="A small local business." * 20,
                ),
                FundingRound(
                    company_id=mega_id, amount_raised=Decimal("500000000")
                ),
                FundingRound(
                    company_id=small_id, amount_raised=Decimal("10000000")
                ),
            ]
        )
        await s1.commit()

    canned = EligibilityJudgment(
        is_startup=False,
        not_startup_reason="Reads as a mature company, not a startup.",
    )
    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_judge_eligibility(committed_session_factory)
    assert summary.companies_judged == 2
    assert summary.prominence_overrides == 1
    assert summary.companies_excluded == 1

    async with committed_session_factory() as s3:
        mega_row = await s3.get(Company, mega_id)
        small_row = await s3.get(Company, small_id)
    # Mega raiser: kept (no exclusion) but still stamped, so the backfill won't
    # re-select it next run.
    assert mega_row is not None
    assert mega_row.exclusion_reason is None
    assert mega_row.eligibility_checked_at is not None
    # Non-prominent company with the same verdict: excluded + stamped as usual.
    assert small_row is not None
    assert small_row.exclusion_reason == "not_a_startup"
    assert small_row.eligibility_checked_at is not None


async def test_prominence_never_overrides_non_us(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prominence overturns ONLY the startup judgment, never nationality
    (review catch): a prominent not-a-startup company whose HQ country is
    non-US is still excluded as non_us."""
    async with committed_session_factory() as s1:
        co = _enriched_company("Ccc Foreign Mega", "prom-judge-foreign")
        co.hq_country = "GB"
        s1.add(co)
        await s1.flush()
        co_id: UUID = co.id
        s1.add_all(
            [
                RawPage(
                    company_id=co_id,
                    url="https://foreign.example/",
                    content="A large established company." * 20,
                ),
                FundingRound(
                    company_id=co_id, amount_raised=Decimal("900000000")
                ),
            ]
        )
        await s1.commit()

    canned = EligibilityJudgment(
        is_startup=False,
        not_startup_reason="Reads as a mature company, not a startup.",
    )
    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_judge_eligibility(committed_session_factory)
    assert summary.prominence_overrides == 1  # the startup verdict WAS overturned
    assert summary.companies_excluded == 1  # …but non_us still fires

    async with committed_session_factory() as s2:
        row = await s2.get(Company, co_id)
    assert row is not None
    assert row.exclusion_reason == "non_us"

    # Idempotent: both rows are stamped, so a second run selects nothing.
    summary2 = await run_judge_eligibility(committed_session_factory)
    assert summary2.companies_judged == 0


async def test_unknown_keeps_company(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with committed_session_factory() as s1:
        co = _enriched_company("Fine Co", "fine-co-judge")
        s1.add(co)
        await s1.commit()
        co_id: UUID = co.id

    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=EligibilityJudgment()),
    )

    summary = await run_judge_eligibility(committed_session_factory)
    assert summary.companies_judged == 1
    assert summary.companies_excluded == 0
    async with committed_session_factory() as s3:
        refetched = await s3.get(Company, co_id)
    assert refetched is not None
    assert refetched.exclusion_reason is None
    assert refetched.eligibility_checked_at is not None


async def test_non_us_judgment_excludes(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The country judgment is half the reason this stage exists; exercise it
    # directly rather than relying on parity with the enrich path.
    async with committed_session_factory() as s1:
        co = _enriched_company("Bangalore Co", "bangalore-co-judge")
        s1.add(co)
        await s1.commit()
        co_id: UUID = co.id

    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(
            return_value=EligibilityJudgment(is_startup=True, hq_country="IN")
        ),
    )

    summary = await run_judge_eligibility(committed_session_factory)
    assert summary.companies_excluded == 1
    async with committed_session_factory() as s3:
        refetched = await s3.get(Company, co_id)
    assert refetched is not None
    assert refetched.exclusion_reason == "non_us"
    assert refetched.hq_country == "IN"


async def test_drains_every_company_each_in_its_own_session(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop still drains the whole selection now that each company runs in
    its own short-lived session (behavior preserved across the refactor)."""
    async with committed_session_factory() as s1:
        cos = [_enriched_company(f"Drain Co {i}", "drain-judge") for i in range(5)]
        s1.add_all(cos)
        await s1.commit()
        ids: list[UUID] = [c.id for c in cos]

    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(
            return_value=EligibilityJudgment(is_startup=True, hq_country="US")
        ),
    )

    summary = await run_judge_eligibility(committed_session_factory)
    assert summary.companies_judged == 5
    assert summary.companies_excluded == 0
    assert summary.llm_failures == 0

    async with committed_session_factory() as s3:
        for cid in ids:
            row = await s3.get(Company, cid)
            assert row is not None
            assert row.eligibility_checked_at is not None


async def test_rate_limit_stops_the_loop(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate-limit on one company stops the whole loop (don't keep hammering the
    free-tier quota) — companies after it are left for the next run. Guards the
    break now that it travels helper -> caller as a raised LLMRateLimitError."""
    async with committed_session_factory() as s1:
        first = _enriched_company("Aaa RL Co", "rl-judge")
        tripped = _enriched_company("Bbb RL Co", "rl-judge")
        never = _enriched_company("Ccc RL Co", "rl-judge")
        s1.add_all([first, tripped, never])
        await s1.commit()
        first_id, tripped_id, never_id = first.id, tripped.id, never.id

    mock = AsyncMock(
        side_effect=[
            EligibilityJudgment(is_startup=True, hq_country="US"),  # Aaa: judged
            LLMRateLimitError("DeepSeek 429: daily quota exhausted"),  # Bbb: break
            # Ccc is never reached.
        ]
    )
    monkeypatch.setattr("nous.pipeline.judge_eligibility.complete_json", mock)

    summary = await run_judge_eligibility(committed_session_factory)
    assert summary.companies_judged == 1
    assert summary.skipped_rate_limited == 1
    assert mock.await_count == 2  # Ccc's LLM call never happened.

    async with committed_session_factory() as s3:
        first_row = await s3.get(Company, first_id)
        tripped_row = await s3.get(Company, tripped_id)
        never_row = await s3.get(Company, never_id)
    assert first_row is not None and first_row.eligibility_checked_at is not None
    # Neither the rate-limited company nor the unreached one is stamped.
    assert tripped_row is not None and tripped_row.eligibility_checked_at is None
    assert never_row is not None and never_row.eligibility_checked_at is None


async def test_wedged_db_op_skips_one_company_and_loop_continues(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the 2026-06-13 hang: a company whose DB commit wedges
    (simulated here by a commit that sleeps past the per-op timeout) is skipped
    and counted as a failure, while the companies BEFORE and AFTER it still drain
    on fresh sessions. One wedged connection must not stall the whole stage."""
    wedge_name = "Mmm Wedged Co"  # sorts between the Aaa/Zzz neighbours
    async with committed_session_factory() as s1:
        before = _enriched_company("Aaa Before Co", "wedge-judge")
        wedged = _enriched_company(wedge_name, "wedge-judge")
        after = _enriched_company("Zzz After Co", "wedge-judge")
        s1.add_all([before, wedged, after])
        await s1.commit()
        before_id, wedged_id, after_id = before.id, wedged.id, after.id

    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(
            return_value=EligibilityJudgment(is_startup=True, hq_country="US")
        ),
    )

    # Wedge ONLY the target company's commit: make it sleep well past the
    # injected per-op timeout. The per-op timeout cancels the sleep, so its real
    # commit never runs and the row stays un-stamped (re-runnable next time).
    real_commit = AsyncSession.commit

    async def _maybe_wedging_commit(self: AsyncSession) -> None:
        if any(
            isinstance(obj, Company) and obj.name == wedge_name
            for obj in self.identity_map.values()
        ):
            await asyncio.sleep(5)
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", _maybe_wedging_commit)

    summary = await run_judge_eligibility(
        committed_session_factory, db_op_timeout=0.2
    )

    assert summary.companies_judged == 2
    assert summary.llm_failures == 1

    async with committed_session_factory() as s3:
        before_row = await s3.get(Company, before_id)
        after_row = await s3.get(Company, after_id)
        wedged_row = await s3.get(Company, wedged_id)
    assert before_row is not None and before_row.eligibility_checked_at is not None
    assert after_row is not None and after_row.eligibility_checked_at is not None
    # Wedged company's mutations were rolled back — nothing stamped, so the next
    # run re-selects it.
    assert wedged_row is not None and wedged_row.eligibility_checked_at is None


async def test_rejudge_signal_path_reexamines_already_judged_leak(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company the looser prompt wrongly KEPT (already judged, still included)
    whose description matches a non-startup signal is re-judged with the
    tightened prompt under rejudge_nonstartup_signals=True, and now excluded.
    A signal-matching company is ONLY re-selected with the flag set — the
    default run leaves it alone (the production cron is unchanged)."""
    judged_at = datetime.now(tz=UTC)
    async with committed_session_factory() as s1:
        leak = _enriched_company("Manta-ish Co", "rejudge-leak")
        # Already judged + still included, but the stored copy gives it away.
        leak.description_short = "An online business directory for local SMBs."
        leak.eligibility_checked_at = judged_at
        s1.add(leak)
        await s1.commit()
        leak_id: UUID = leak.id

    canned = EligibilityJudgment(
        is_startup=False,
        not_startup_reason="Online business directory, not a software product.",
    )
    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=canned),
    )

    # Default run: the row is already stamped and not a signal target, so it is
    # NOT re-selected — behaviour for the production cron is unchanged.
    default_summary = await run_judge_eligibility(committed_session_factory)
    assert default_summary.companies_judged == 0
    async with committed_session_factory() as s2:
        still = await s2.get(Company, leak_id)
    assert still is not None and still.exclusion_reason is None

    # Opt-in re-judge: the stamp is reset, the row is re-judged, now excluded.
    rejudge_summary = await run_judge_eligibility(
        committed_session_factory, rejudge_nonstartup_signals=True
    )
    assert rejudge_summary.companies_judged == 1
    assert rejudge_summary.companies_excluded == 1
    async with committed_session_factory() as s3:
        refetched = await s3.get(Company, leak_id)
    assert refetched is not None
    assert refetched.exclusion_reason == "not_a_startup"
    assert refetched.eligibility_checked_at is not None


async def test_rejudge_signal_path_leaves_clean_company_untouched(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real startup that was already judged and whose description carries NO
    non-startup signal is never re-selected, even with the flag on — the
    re-judge is targeted, so genuine companies don't get re-LLM'd (precision +
    cost)."""
    judged_at = datetime.now(tz=UTC)
    async with committed_session_factory() as s1:
        good = _enriched_company("Real SaaS Co", "rejudge-clean")
        good.description_short = "A developer platform for shipping APIs faster."
        good.eligibility_checked_at = judged_at
        s1.add(good)
        await s1.commit()
        good_id: UUID = good.id

    mock = AsyncMock(return_value=EligibilityJudgment(is_startup=True))
    monkeypatch.setattr("nous.pipeline.judge_eligibility.complete_json", mock)

    summary = await run_judge_eligibility(
        committed_session_factory, rejudge_nonstartup_signals=True
    )
    assert summary.companies_judged == 0
    assert mock.await_count == 0  # no LLM call — the clean row was never picked.
    async with committed_session_factory() as s3:
        refetched = await s3.get(Company, good_id)
    assert refetched is not None
    assert refetched.exclusion_reason is None
    # Stamp untouched (still the original judged_at, not reset).
    assert refetched.eligibility_checked_at is not None
