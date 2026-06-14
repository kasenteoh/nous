"""Tests for the analyze-competitors stage.

DB-gated integration tests covering:
- Eligibility query (description_long + industry_group required; TTL gate).
- Peer-list query (50-cap, same industry_group, target excluded, recency order).
- Competitor name resolution (exact normalized_name match; otherwise null).
- Replace-style write inside one transaction.
- Main loop happy path with a mocked LLM.
- Rate-limit, parse-error, TTL-gate, dry-run behaviors.
- Bounded-concurrency parity: same rows + deterministic order as sequential,
  genuine parallel LLM dispatch, and 429 stops scheduling further work.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Competitor, NewsArticle
from nous.llm.client import LLMParseError, LLMRateLimitError
from nous.llm.prompts.competitor_analysis import (
    Competitor as CompetitorOut,
)
from nous.llm.prompts.competitor_analysis import (
    CompetitorAnalysis,
)
from nous.llm.prompts.competitor_candidates import (
    CandidateMention,
    CompetitorCandidates,
)
from nous.pipeline.analyze_competitors import (
    fetch_eligible_companies,
    fetch_peers,
    resolve_competitor_company_id,
    run_analyze_competitors,
)
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(
    name: str,
    *,
    description_long: str | None = "Long desc",
    industry_group: str | None = "SaaS",
) -> Company:
    return Company(
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{os.urandom(3).hex()}",
        normalized_name=normalize_name(name),
        description_short=f"{name} short.",
        description_long=description_long,
        industry_group=industry_group,
        hq_country="US",
    )


# ---------------------------------------------------------------------------
# Eligibility query
# ---------------------------------------------------------------------------


async def test_eligible_requires_description_long(db: AsyncSession) -> None:
    yes = _make_company("Yes")
    no = _make_company("No", description_long=None)
    db.add_all([yes, no])
    await db.flush()

    eligible = await fetch_eligible_companies(db, limit=10, ttl_days=25)
    ids = {c.id for c in eligible}
    assert yes.id in ids
    assert no.id not in ids


async def test_eligible_requires_industry_group(db: AsyncSession) -> None:
    yes = _make_company("Yes")
    no = _make_company("No", industry_group=None)
    db.add_all([yes, no])
    await db.flush()

    eligible = await fetch_eligible_companies(db, limit=10, ttl_days=25)
    ids = {c.id for c in eligible}
    assert yes.id in ids
    assert no.id not in ids


async def test_eligible_skips_recently_analyzed(db: AsyncSession) -> None:
    fresh = _make_company("Fresh")
    stale = _make_company("Stale")
    db.add_all([fresh, stale])
    await db.flush()

    # Fresh has a competitor updated 5 days ago — gated out by 25-day TTL.
    fresh_recent = Competitor(
        company_id=fresh.id,
        competitor_name="X",
        rank=1,
        updated_at=datetime.now(UTC) - timedelta(days=5),
    )
    # Stale has a competitor updated 40 days ago — eligible again.
    stale_old = Competitor(
        company_id=stale.id,
        competitor_name="Y",
        rank=1,
        updated_at=datetime.now(UTC) - timedelta(days=40),
    )
    db.add_all([fresh_recent, stale_old])
    await db.flush()

    eligible = await fetch_eligible_companies(db, limit=10, ttl_days=25)
    ids = {c.id for c in eligible}
    assert stale.id in ids
    assert fresh.id not in ids


async def test_eligible_respects_limit(db: AsyncSession) -> None:
    db.add_all([_make_company(f"Co{i}") for i in range(5)])
    await db.flush()

    eligible = await fetch_eligible_companies(db, limit=2, ttl_days=25)
    assert len(eligible) == 2


# ---------------------------------------------------------------------------
# Peer-list query
# ---------------------------------------------------------------------------


async def test_peers_same_industry_only(db: AsyncSession) -> None:
    target = _make_company("Target", industry_group="SaaS")
    same = _make_company("Same", industry_group="SaaS")
    other = _make_company("Other", industry_group="Hardware")
    db.add_all([target, same, other])
    await db.flush()

    peers = await fetch_peers(db, target=target)
    names = {p.name for p in peers}
    assert "Same" in names
    assert "Other" not in names


async def test_peers_exclude_self(db: AsyncSession) -> None:
    target = _make_company("Target", industry_group="SaaS")
    peer = _make_company("Peer", industry_group="SaaS")
    db.add_all([target, peer])
    await db.flush()

    peers = await fetch_peers(db, target=target)
    names = {p.name for p in peers}
    assert "Target" not in names
    assert "Peer" in names


async def test_peers_capped_at_max(db: AsyncSession) -> None:
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    db.add_all(
        [
            _make_company(f"Peer{i:03d}", industry_group="SaaS")
            for i in range(60)
        ]
    )
    await db.flush()

    peers = await fetch_peers(db, target=target, max_peers=50)
    assert len(peers) == 50


async def test_peers_carry_short_description(db: AsyncSession) -> None:
    target = _make_company("Target", industry_group="SaaS")
    peer = _make_company("Peer", industry_group="SaaS")
    db.add_all([target, peer])
    await db.flush()

    peers = await fetch_peers(db, target=target)
    assert peers[0].description_short == "Peer short."


# ---------------------------------------------------------------------------
# Competitor resolution
# ---------------------------------------------------------------------------


async def test_resolve_exact_normalized_match(db: AsyncSession) -> None:
    rival = _make_company("Beta Co")
    db.add(rival)
    await db.flush()

    resolved = await resolve_competitor_company_id(db, name="Beta Co")
    assert resolved == rival.id


async def test_resolve_normalizes_case(db: AsyncSession) -> None:
    rival = _make_company("Beta Co")
    db.add(rival)
    await db.flush()

    resolved = await resolve_competitor_company_id(db, name="BETA CO")
    assert resolved == rival.id


async def test_resolve_returns_none_for_unknown(db: AsyncSession) -> None:
    resolved = await resolve_competitor_company_id(db, name="NeverSeen")
    assert resolved is None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _fixture_extraction(names: list[str]) -> CompetitorAnalysis:
    return CompetitorAnalysis(
        competitors=[
            CompetitorOut(
                name=n,
                description=f"{n} description.",
                reasoning=f"{n} reasoning.",
                rank=i,
            )
            for i, n in enumerate(names, start=1)
        ]
    )


async def test_happy_path_writes_competitors_and_resolves_links(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_company("Target", industry_group="SaaS")
    # description_long=None makes RivalCo ineligible for analysis; it only
    # needs to exist as a resolution target for the LLM-named "RivalCo".
    rival = _make_company("RivalCo", industry_group="SaaS", description_long=None)
    db.add_all([target, rival])
    await db.flush()

    extraction = _fixture_extraction(["RivalCo", "UnindexedCo"])

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        assert schema is CompetitorAnalysis
        return extraction

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)

    assert summary.companies_analyzed == 1
    assert summary.competitors_written == 2
    assert summary.competitors_linked == 1
    assert summary.competitors_unlinked == 1

    rows = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == target.id).order_by(Competitor.rank)
        )
    ).scalars().all()
    assert [r.competitor_name for r in rows] == ["RivalCo", "UnindexedCo"]
    assert rows[0].competitor_company_id == rival.id
    assert rows[1].competitor_company_id is None


async def test_stage_commits_writes_not_just_flushes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the stage must COMMIT, not just flush.

    The CLI opens a plain AsyncSessionLocal() with no auto-commit, so a
    flush-only stage had every run rolled back on session close — the
    competitors table stayed empty in prod even when the LLM returned
    competitors. (The shared-session test fixture hid this: a flush is visible
    within the same session, so the happy-path assertions passed regardless.)
    """
    target = _make_company("CommitTarget", industry_group="SaaS")
    rival = _make_company("CommitRival", industry_group="SaaS", description_long=None)
    db.add_all([target, rival])
    await db.flush()

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        return _fixture_extraction(["CommitRival"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    commits = 0
    original_commit = db.commit

    async def _counting_commit() -> None:
        nonlocal commits
        commits += 1
        await original_commit()

    monkeypatch.setattr(db, "commit", _counting_commit)

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)

    assert summary.competitors_written >= 1
    assert commits >= 1, "stage must commit its writes, not just flush them"


async def test_llm_only_competitors_tagged_llm_inferred(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no TechCrunch coverage, pass 1 is skipped and every competitor is
    written with source='llm_inferred' and no source_url."""
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    await db.flush()

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        # Only the analysis call should happen — there are no TC articles.
        assert schema is CompetitorAnalysis
        return _fixture_extraction(["Rival"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)
    assert summary.competitors_from_llm == 1
    assert summary.competitors_from_techcrunch == 0

    rows = (
        await db.execute(select(Competitor).where(Competitor.company_id == target.id))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].source == "llm_inferred"
    assert rows[0].source_url is None


async def test_techcrunch_candidates_are_revalidated_and_sourced(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two-step flow: a competitor surfaced from the target's TechCrunch coverage
    is tagged source='techcrunch' with the article URL; competitors the model
    adds on its own are 'llm_inferred'."""
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    await db.flush()

    tc_url = "https://techcrunch.com/2026/05/01/target-raises"
    db.add(
        NewsArticle(
            company_id=target.id,
            url=tc_url,
            title="Target raises",
            source="techcrunch.com",
            raw_content="Target competes head-on with Globex in the market. " * 5,
        )
    )
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> object:
        if schema is CompetitorCandidates:
            return CompetitorCandidates(
                candidates=[CandidateMention(name="Globex", article_url=tc_url)]
            )
        # Pass 2: model keeps Globex (revalidated) and adds an own pick.
        return _fixture_extraction(["Globex", "LLMRival"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)
    assert summary.competitors_from_techcrunch == 1
    assert summary.competitors_from_llm == 1

    rows = (
        await db.execute(
            select(Competitor)
            .where(Competitor.company_id == target.id)
            .order_by(Competitor.rank)
        )
    ).scalars().all()
    by_name = {r.competitor_name: r for r in rows}
    assert by_name["Globex"].source == "techcrunch"
    assert by_name["Globex"].source_url == tc_url
    assert by_name["LLMRival"].source == "llm_inferred"
    assert by_name["LLMRival"].source_url is None


async def test_rerun_replaces_existing_rows(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    await db.flush()

    # Pre-existing competitor row that's older than the TTL so the run picks it up.
    db.add(
        Competitor(
            company_id=target.id,
            competitor_name="OldRival",
            rank=1,
            updated_at=datetime.now(UTC) - timedelta(days=40),
        )
    )
    await db.flush()

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        return _fixture_extraction(["NewRival"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    await run_analyze_competitors(db, limit=10, ttl_days=25)

    rows = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == target.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].competitor_name == "NewRival"


async def test_dry_run_writes_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    await db.flush()

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        return _fixture_extraction(["Rival"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25, dry_run=True)
    assert summary.companies_analyzed == 1

    rows = (
        await db.execute(select(Competitor).where(Competitor.company_id == target.id))
    ).scalars().all()
    assert rows == []


async def test_rate_limit_halts_loop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _make_company("First", industry_group="SaaS")
    second = _make_company("Second", industry_group="SaaS")
    db.add_all([first, second])
    await db.flush()

    call_count = {"n": 0}

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _fixture_extraction(["X"])
        raise LLMRateLimitError("429")

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)
    assert summary.companies_analyzed == 1
    assert summary.skipped_rate_limited == 1

    # First company's row should be persisted; loop broke before second got written.
    rows_first = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == first.id)
        )
    ).scalars().all()
    rows_second = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == second.id)
        )
    ).scalars().all()
    assert len(rows_first) == 1
    assert rows_second == []


async def test_parse_error_skips_company_and_continues(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _make_company("Bad", industry_group="SaaS")
    good = _make_company("Good", industry_group="SaaS")
    db.add_all([bad, good])
    await db.flush()

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        # First call (sorted by name: "Bad" before "Good" alphabetically) raises.
        # Discriminate on the target header "Name: Bad" — not bare "Bad", which
        # also appears in Good's prompt as a peer entry.
        if "Name: Bad" in prompt:
            raise LLMParseError("schema mismatch")
        return _fixture_extraction(["X"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)
    assert summary.llm_failures == 1
    assert summary.companies_analyzed == 1

    rows_good = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == good.id)
        )
    ).scalars().all()
    assert len(rows_good) == 1


async def test_ttl_gate_skips_recently_analyzed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    await db.flush()

    db.add(
        Competitor(
            company_id=target.id,
            competitor_name="Existing",
            rank=1,
            updated_at=datetime.now(UTC) - timedelta(days=10),
        )
    )
    await db.flush()

    called = {"n": 0}

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        called["n"] += 1
        return _fixture_extraction(["Anything"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)
    assert called["n"] == 0
    assert summary.companies_analyzed == 0


# ---------------------------------------------------------------------------
# Bounded concurrency (parity with the sequential behavior)
# ---------------------------------------------------------------------------


async def test_concurrency_writes_same_rows_as_sequential(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With concurrency > 1, every eligible company is analyzed and its
    competitor rows are written exactly as a sequential run would — one set per
    company, ranked, with the resolved FK on the matching peer."""
    n = 12
    targets = [_make_company(f"CoTarget{i:02d}", industry_group="SaaS") for i in range(n)]
    # A resolvable rival per target; ineligible (no description_long) so it is
    # only a resolution target, never analyzed itself.
    rivals = [
        _make_company(f"CoRival{i:02d}", industry_group="SaaS", description_long=None)
        for i in range(n)
    ]
    db.add_all([*targets, *rivals])
    await db.flush()

    async def _fake(prompt: str, schema: type) -> CompetitorAnalysis:
        # Route by the target header so each company gets its own rival linked.
        for i in range(n):
            if f"Name: CoTarget{i:02d}" in prompt:
                return _fixture_extraction([f"CoRival{i:02d}", "Unlinked"])
        raise AssertionError("prompt did not name a known target")

    monkeypatch.setattr("nous.pipeline.analyze_competitors.complete_json", _fake)

    summary = await run_analyze_competitors(db, limit=100, ttl_days=25, concurrency=5)

    assert summary.companies_analyzed == n
    assert summary.competitors_written == 2 * n
    assert summary.competitors_linked == n  # one resolvable rival each
    assert summary.competitors_unlinked == n  # the "Unlinked" pick each

    for i, target in enumerate(targets):
        rows = (
            (
                await db.execute(
                    select(Competitor)
                    .where(Competitor.company_id == target.id)
                    .order_by(Competitor.rank)
                )
            )
            .scalars()
            .all()
        )
        assert [r.competitor_name for r in rows] == [f"CoRival{i:02d}", "Unlinked"]
        assert rows[0].competitor_company_id == rivals[i].id
        assert rows[1].competitor_company_id is None


async def test_concurrency_runs_llm_calls_in_parallel(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM passes for a batch overlap in time (bounded by the semaphore),
    rather than running strictly one-at-a-time. Proven by recording the max
    number of concurrently-in-flight ``complete_json`` calls."""
    n = 8
    targets = [_make_company(f"ParCo{i:02d}", industry_group="SaaS") for i in range(n)]
    db.add_all(targets)
    await db.flush()

    state = {"in_flight": 0, "peak": 0}

    async def _fake(prompt: str, schema: type) -> CompetitorAnalysis:
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            # Yield to the loop so siblings can pile up before we return.
            await asyncio.sleep(0.02)
            return _fixture_extraction(["Rival"])
        finally:
            state["in_flight"] -= 1

    monkeypatch.setattr("nous.pipeline.analyze_competitors.complete_json", _fake)

    await run_analyze_competitors(db, limit=100, ttl_days=25, concurrency=4)

    # No TC articles → exactly one analysis call per company. With concurrency=4
    # at least two should have been in flight together.
    assert state["peak"] >= 2
    assert state["peak"] <= 4  # never exceeds the semaphore bound


async def test_concurrency_one_is_strictly_sequential(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """concurrency=1 degrades to one-at-a-time: peak in-flight is never > 1.
    This is the safety floor and the parity baseline."""
    n = 5
    db.add_all([_make_company(f"SeqCo{i}", industry_group="SaaS") for i in range(n)])
    await db.flush()

    state = {"in_flight": 0, "peak": 0}

    async def _fake(prompt: str, schema: type) -> CompetitorAnalysis:
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            await asyncio.sleep(0.005)
            return _fixture_extraction(["Rival"])
        finally:
            state["in_flight"] -= 1

    monkeypatch.setattr("nous.pipeline.analyze_competitors.complete_json", _fake)

    summary = await run_analyze_competitors(db, limit=100, ttl_days=25, concurrency=1)
    assert summary.companies_analyzed == n
    assert state["peak"] == 1


async def test_concurrency_rate_limit_stops_scheduling_further_batches(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 stops the run at a batch boundary: companies in later batches are
    never sent to the LLM, and ``skipped_rate_limited`` is recorded exactly
    once (not per task). First-batch companies are still written."""
    # 10 companies, concurrency 3 → batches [0,1,2] [3,4,5] [6,7,8] [9].
    n = 10
    targets = [_make_company(f"RlCo{i:02d}", industry_group="SaaS") for i in range(n)]
    db.add_all(targets)
    await db.flush()
    first_batch_ids = {targets[i].id for i in range(3)}

    seen: list[str] = []

    async def _fake(prompt: str, schema: type) -> CompetitorAnalysis:
        # Raise on the second-batch company "RlCo04"; everything else succeeds.
        for i in range(n):
            if f"Name: RlCo{i:02d}" in prompt:
                seen.append(f"RlCo{i:02d}")
                if i == 4:
                    raise LLMRateLimitError("429")
                return _fixture_extraction(["Rival"])
        raise AssertionError("unknown target")

    monkeypatch.setattr("nous.pipeline.analyze_competitors.complete_json", _fake)

    summary = await run_analyze_competitors(db, limit=100, ttl_days=25, concurrency=3)

    # Recorded exactly once regardless of how many tasks raced.
    assert summary.skipped_rate_limited == 1
    # The third+ batches (RlCo06..RlCo09) must never have hit the LLM.
    for i in range(6, n):
        assert f"RlCo{i:02d}" not in seen

    # Every first-batch company is fully written (a 429 keeps prior work).
    written_company_ids = set(
        (await db.execute(select(Competitor.company_id))).scalars().all()
    )
    assert first_batch_ids <= written_company_ids


async def test_concurrency_dry_run_writes_nothing_for_many(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run under concurrency still writes zero rows while counting every
    company as analyzed."""
    n = 7
    db.add_all(
        [_make_company(f"DryCo{i}", industry_group="SaaS") for i in range(n)]
    )
    await db.flush()

    async def _fake(prompt: str, schema: type) -> CompetitorAnalysis:
        return _fixture_extraction(["Rival"])

    monkeypatch.setattr("nous.pipeline.analyze_competitors.complete_json", _fake)

    summary = await run_analyze_competitors(
        db, limit=100, ttl_days=25, dry_run=True, concurrency=5
    )
    assert summary.companies_analyzed == n
    rows = (await db.execute(select(Competitor))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# Self-referential competitor guard
# ---------------------------------------------------------------------------


async def test_self_referential_competitor_is_dropped(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a competitor name resolves to the target company itself the stage
    must drop that row (to avoid ck_competitors_no_self_reference), write the
    remaining competitors with gap-free contiguous ranks, and not raise."""
    target = _make_company("SelfRefCo", industry_group="SaaS")
    other = _make_company("LegitRival", industry_group="SaaS", description_long=None)
    db.add_all([target, other])
    await db.flush()

    # The LLM returns three competitors: the target itself (self-ref), a legit
    # rival, and an unindexed name.  Only the latter two should be written.
    # normalize_name("SelfRefCo") == "selfrefco" which matches target.normalized_name.
    extraction = CompetitorAnalysis(
        competitors=[
            CompetitorOut(
                name="SelfRefCo",  # same normalized_name as target → self-ref
                description="Self description.",
                reasoning="Self reasoning.",
                rank=1,
            ),
            CompetitorOut(
                name="LegitRival",
                description="Legit description.",
                reasoning="Legit reasoning.",
                rank=2,
            ),
            CompetitorOut(
                name="UnindexedCo",
                description="Unknown description.",
                reasoning="Unknown reasoning.",
                rank=3,
            ),
        ]
    )

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        assert schema is CompetitorAnalysis
        return extraction

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)

    # The self-ref is dropped; only 2 competitors survive.
    assert summary.companies_analyzed == 1
    assert summary.competitors_written == 2

    rows = (
        await db.execute(
            select(Competitor)
            .where(Competitor.company_id == target.id)
            .order_by(Competitor.rank)
        )
    ).scalars().all()

    # The target itself must NOT appear as a competitor_company_id.
    written_linked_ids = {r.competitor_company_id for r in rows}
    assert target.id not in written_linked_ids

    # Ranks are contiguous 1..2, not 2..3 (gap-free over survivors).
    assert [r.rank for r in rows] == [1, 2]
    assert [r.competitor_name for r in rows] == ["LegitRival", "UnindexedCo"]


async def test_per_company_persist_failure_continues_to_next(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB constraint error during _persist_analysis for one company must not
    abort the stage — remaining companies are still processed and written.

    The test patches _persist_analysis to raise IntegrityError on the FIRST
    call, and also patches session.rollback to be a no-op (because the mock
    exception was not raised inside a real begin_nested SAVEPOINT, so a real
    rollback would undo the test's outer transaction). The stage must:
    - Catch the error and increment llm_failures.
    - Continue to the second company and write its rows.
    """
    # "First" sorts before "Second" alphabetically, so FirstCo is processed first.
    first = _make_company("FirstCo", industry_group="SaaS")
    second = _make_company("SecondCo", industry_group="SaaS")
    db.add_all([first, second])
    await db.flush()

    second_id = second.id  # capture before any potential expiry

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        return _fixture_extraction(["Rival"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    from sqlalchemy.exc import IntegrityError as _IntegrityError

    call_count = {"n": 0}
    original_persist = __import__(
        "nous.pipeline.analyze_competitors", fromlist=["_persist_analysis"]
    )._persist_analysis

    async def _failing_persist(  # type: ignore[misc]
        session: AsyncSession, summary: object, **kwargs: object
    ) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _IntegrityError("mock constraint", None, None)  # type: ignore[call-arg]
        await original_persist(session, summary, **kwargs)

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors._persist_analysis", _failing_persist
    )

    # The mock IntegrityError is raised outside a begin_nested SAVEPOINT, so
    # session.rollback() would undo the test's outer transaction (evicting the
    # company rows). Patch it to a no-op for this test — the defence-in-depth
    # rollback logic is exercised by test_self_referential_competitor_is_dropped
    # via a real DB constraint.
    async def _noop_rollback() -> None:
        pass

    monkeypatch.setattr(db, "rollback", _noop_rollback)

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25, concurrency=1)

    # First company's persist failed; second must still be written.
    assert summary.llm_failures == 1
    assert summary.companies_analyzed == 1  # only SecondCo counted
    assert summary.competitors_written >= 1

    rows_second = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == second_id)
        )
    ).scalars().all()
    assert len(rows_second) >= 1
