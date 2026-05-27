"""Tests for the analyze-competitors stage.

DB-gated integration tests covering:
- Eligibility query (description_long + industry_group required; TTL gate).
- Peer-list query (50-cap, same industry_group, target excluded, recency order).
- Competitor name resolution (exact normalized_name match; otherwise null).
- Replace-style write inside one transaction.
- Main loop happy path with a mocked LLM.
- Rate-limit, parse-error, TTL-gate, dry-run behaviors.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Competitor
from nous.llm.client import LLMParseError, LLMRateLimitError
from nous.llm.prompts.competitor_analysis import (
    Competitor as CompetitorOut,
)
from nous.llm.prompts.competitor_analysis import (
    CompetitorAnalysis,
)
from nous.pipeline.analyze_competitors import (
    fetch_eligible_companies,
    fetch_peers,
    resolve_competitor_company_id,
    run_analyze_competitors,
)

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
        normalized_name=name.lower(),
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
