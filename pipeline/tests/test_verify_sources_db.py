"""DB-gated integration tests for the source-verification prevalence probe and
the stored-text fact collection.

Requires DATABASE_URL (Postgres with `alembic upgrade head` applied); skipped
otherwise. Seeds companies + funding rounds + news_articles and asserts the
verifiability buckets and the (no-LLM) fact selection. The LLM dry-run itself is
not exercised here — it needs the DeepSeek key (Actions only).
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification, FundingRound, NewsArticle
from nous.llm.prompts.source_verification import PROMPT_VERSION
from nous.pipeline.verify_sources import (
    _collect_stored_text_facts,
    _Fact,
    _upsert_verdict,
    funding_round_claim,
    run_verify_sources_probe,
    total_raised_claim,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_BODY = "TechCrunch reports the round in detail. " * 10  # comfortably ≥ 200 chars


def _company(name: str, **kwargs: object) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{name.lower()}-{suffix}",
        normalized_name=f"{name.lower()}{suffix}",
        description_short="A shown company.",  # shown regardless of funding
        **kwargs,  # type: ignore[arg-type]
    )


def _news(company_id: object, url: str) -> NewsArticle:
    return NewsArticle(
        company_id=company_id,
        url=url,
        title="Round announced",
        source="techcrunch.com",
        raw_content=_BODY,
    )


async def test_probe_buckets_and_fact_collection(db: AsyncSession) -> None:
    tc_a = "https://techcrunch.com/acme-series-a"
    tc_e = "https://techcrunch.com/echo-acquired"
    gnews = "https://news.google.com/rss/articles/CBMiOpaqueRedirect"
    reuters = "https://reuters.com/charlie-round"

    # A — shown; total_raised + a funding round both cite a STORED news article.
    a = _company(
        "Alpha",
        latest_round_amount=Decimal("300000000"),
        total_raised_usd=Decimal("12000000"),
        total_raised_source_url=tc_a,
    )
    # B — shown; a funding round cites a bare Google News redirect (no stored text).
    b = _company("Bravo", latest_round_amount=Decimal("200000000"))
    # C — shown; a funding round cites a real host with NO stored text → refetch.
    c = _company("Charlie", latest_round_amount=Decimal("150000000"))
    # D — EXCLUDED: sourced facts here must be counted nowhere.
    d = _company(
        "Delta",
        exclusion_reason="non_us",
        total_raised_usd=Decimal("900000000"),
        total_raised_source_url=tc_a,
        status="acquired",
        status_source_url=tc_e,
    )
    # E — shown; a non-active status cites a STORED news article.
    e = _company(
        "Echo",
        latest_round_amount=Decimal("500000000"),
        status="acquired",
        status_source_url=tc_e,
    )
    for co in (a, b, c, d, e):
        db.add(co)
    await db.flush()

    db.add(_news(a.id, tc_a))
    db.add(_news(e.id, tc_e))
    db.add(FundingRound(company_id=a.id, primary_news_url=tc_a, amount_raised=Decimal("12000000")))
    db.add(FundingRound(company_id=b.id, primary_news_url=gnews))
    db.add(FundingRound(company_id=c.id, primary_news_url=reuters))
    db.add(FundingRound(company_id=d.id, primary_news_url=tc_a))  # excluded → ignored
    await db.flush()

    summary = await run_verify_sources_probe(db)

    # total_raised: A stored (D excluded).
    assert summary.buckets_by_kind["total_raised"]["stored"] == 1
    assert summary.facts_by_kind["total_raised"] == 1
    # status: E stored (D excluded).
    assert summary.buckets_by_kind["status"]["stored"] == 1
    assert summary.facts_by_kind["status"] == 1
    # funding_round: A stored, B unreachable, C refetch (D excluded).
    fr = summary.buckets_by_kind["funding_round"]
    assert fr["stored"] == 1
    assert fr["unreachable"] == 1
    assert fr["refetch"] == 1
    assert summary.facts_by_kind["funding_round"] == 3

    # Aggregates: 3 stored, 1 refetch, 1 unreachable, 0 unparseable.
    assert summary.stored == 3
    assert summary.refetch == 1
    assert summary.unreachable == 1
    assert summary.unparseable == 0
    assert summary.total_facts == 5
    assert summary.addressable == 4

    # Fact collection returns exactly the 3 stored-text facts, each with a claim
    # and loaded source text (no LLM).
    facts = await _collect_stored_text_facts(db, limit=10)
    kinds = sorted((f.company_name, f.fact_kind) for f in facts)
    assert kinds == [
        ("Alpha", "funding_round"),
        ("Alpha", "total_raised"),
        ("Echo", "status"),
    ]
    for f in facts:
        assert f.claim  # a non-empty claim string
        assert len(f.source_text) >= 200  # stored body loaded and truncated


async def test_probe_empty_cohort_is_zeroed(db: AsyncSession) -> None:
    # No sourced facts at all → all counts zero, no division-by-zero.
    summary = await run_verify_sources_probe(db)
    assert summary.total_facts == 0
    assert summary.addressable_pct == 0.0
    assert summary.stored_pct == 0.0


# ── apply path ────────────────────────────────────────────────────────────────


async def test_apply_gate_excludes_verified_reselects_stale(db: AsyncSession) -> None:
    tc = "https://techcrunch.com/acme-tr"
    a = _company(
        "Acme",
        latest_round_amount=Decimal("100000000"),
        total_raised_usd=Decimal("12000000"),
        total_raised_source_url=tc,
    )
    db.add(a)
    await db.flush()
    db.add(_news(a.id, tc))
    await db.flush()

    def _has_tr(facts: list[_Fact]) -> bool:
        return any(
            f.company_name == "Acme" and f.fact_kind == "total_raised" for f in facts
        )

    # Unverified → apply collection includes it.
    assert _has_tr(await _collect_stored_text_facts(db, limit=10, for_apply=True))

    # Verify at the CURRENT version + same source + the claim rendered today →
    # excluded from apply selection…
    db.add(
        FactVerification(
            company_id=a.id,
            fact_kind="total_raised",
            fact_ref="",
            source_url=tc,
            claim=total_raised_claim("Acme", Decimal("12000000"), None),
            verdict="supported",
            supporting_quote="q",
            prompt_version=PROMPT_VERSION,
        )
    )
    await db.flush()
    assert not _has_tr(await _collect_stored_text_facts(db, limit=10, for_apply=True))
    # …but dry-run collection ignores the gate (still verifies it).
    assert _has_tr(await _collect_stored_text_facts(db, limit=10, for_apply=False))

    # A claim that drifts under the SAME version + source (a corrected amount
    # re-extracted from the same article — the #199 known gap) re-selects via
    # the stale-claim sweep.
    fv = (await db.execute(select(FactVerification))).scalar_one()
    fv.claim = "Acme has raised a total of $9.0M."
    await db.flush()
    assert _has_tr(await _collect_stored_text_facts(db, limit=10, for_apply=True))
    fv.claim = total_raised_claim("Acme", Decimal("12000000"), None)
    await db.flush()
    assert not _has_tr(await _collect_stored_text_facts(db, limit=10, for_apply=True))

    # A stale verification (old prompt version) re-selects it in apply mode.
    fv.prompt_version = "2000-01-01.0"
    await db.flush()
    assert _has_tr(await _collect_stored_text_facts(db, limit=10, for_apply=True))


async def test_apply_gate_reselects_on_source_change(db: AsyncSession) -> None:
    tc = "https://techcrunch.com/nimbus-current"
    n = _company(
        "Nimbus",
        latest_round_amount=Decimal("90000000"),
        total_raised_usd=Decimal("30000000"),
        total_raised_source_url=tc,
    )
    db.add(n)
    await db.flush()
    db.add(_news(n.id, tc))
    # A verification at the CURRENT version but a DIFFERENT (old) source_url →
    # the fact re-selects (the fact now cites a new article).
    db.add(
        FactVerification(
            company_id=n.id,
            fact_kind="total_raised",
            fact_ref="",
            source_url="https://old-source.example/stale",
            claim="prior claim",
            verdict="supported",
            supporting_quote="q",
            prompt_version=PROMPT_VERSION,
        )
    )
    await db.flush()
    facts = await _collect_stored_text_facts(db, limit=10, for_apply=True)
    assert any(
        f.company_name == "Nimbus" and f.fact_kind == "total_raised" for f in facts
    )


async def test_upsert_verdict_inserts_then_updates(db: AsyncSession) -> None:
    b = _company("Beta")
    db.add(b)
    await db.flush()
    fact = _Fact(
        company_id=b.id,
        company_slug=b.slug,
        company_name="Beta",
        fact_kind="status",
        fact_label="status: acquired",
        source_url="https://techcrunch.com/beta",
        claim="Beta has been acquired.",
        prominence=0.0,
        source_text=_BODY,
    )
    await _upsert_verdict(db, fact, verdict="supported", quote="acquired by X")
    await db.flush()
    row = (
        await db.execute(
            select(FactVerification).where(FactVerification.company_id == b.id)
        )
    ).scalar_one()
    assert row.verdict == "supported"
    assert row.fact_ref == ""
    assert row.supporting_quote == "acquired by X"

    # Re-upsert (same company/kind/ref) with a new verdict → SAME row updated.
    await _upsert_verdict(db, fact, verdict="unsupported", quote=None)
    await db.flush()
    rows = (
        await db.execute(
            select(FactVerification).where(FactVerification.company_id == b.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].verdict == "unsupported"
    assert rows[0].supporting_quote is None


async def test_null_amount_round_is_skipped(db: AsyncSession) -> None:
    tc = "https://techcrunch.com/gamma"
    g = _company("Gamma", latest_round_amount=Decimal("50000000"))
    db.add(g)
    await db.flush()
    db.add(_news(g.id, tc))
    db.add(FundingRound(company_id=g.id, primary_news_url=tc, amount_raised=None))
    await db.flush()
    facts = await _collect_stored_text_facts(db, limit=10, for_apply=False)
    assert not any(
        f.fact_kind == "funding_round" and f.company_name == "Gamma" for f in facts
    )


async def test_apply_upsert_carries_round_fact_ref(db: AsyncSession) -> None:
    tc = "https://techcrunch.com/delta-round"
    d = _company("Delta", latest_round_amount=Decimal("80000000"))
    db.add(d)
    await db.flush()
    db.add(_news(d.id, tc))
    fr = FundingRound(
        company_id=d.id, primary_news_url=tc, amount_raised=Decimal("40000000")
    )
    db.add(fr)
    await db.flush()
    [fact] = [
        f
        for f in await _collect_stored_text_facts(db, limit=10, for_apply=True)
        if f.fact_kind == "funding_round"
    ]
    assert fact.fact_ref == str(fr.id)  # the round's id keys the fact_ref
    await _upsert_verdict(db, fact, verdict="uncertain", quote=None)
    await db.flush()
    row = (
        await db.execute(
            select(FactVerification).where(FactVerification.fact_kind == "funding_round")
        )
    ).scalar_one()
    assert row.fact_ref == str(fr.id)


async def test_stale_claim_sweep_reselects_drifted_round(db: AsyncSession) -> None:
    """A round verified at the current version + source whose amount then changes
    (same article) re-selects via the stale-claim sweep; a matching claim stays
    excluded."""
    tc = "https://techcrunch.com/epsilon-round"
    e = _company("Epsilon", latest_round_amount=Decimal("60000000"))
    db.add(e)
    await db.flush()
    db.add(_news(e.id, tc))
    fr = FundingRound(
        company_id=e.id,
        primary_news_url=tc,
        amount_raised=Decimal("60000000"),
        round_type="Series B",
    )
    db.add(fr)
    await db.flush()

    db.add(
        FactVerification(
            company_id=e.id,
            fact_kind="funding_round",
            fact_ref=str(fr.id),
            source_url=tc,
            claim=funding_round_claim(
                "Epsilon", Decimal("60000000"), "Series B", None, None
            ),
            verdict="supported",
            supporting_quote="q",
            prompt_version=PROMPT_VERSION,
        )
    )
    await db.flush()

    def _round_facts(facts: list[_Fact]) -> list[_Fact]:
        return [
            f
            for f in facts
            if f.fact_kind == "funding_round" and f.company_name == "Epsilon"
        ]

    # Claim matches what nous renders today → gated out, no re-bill.
    assert not _round_facts(
        await _collect_stored_text_facts(db, limit=10, for_apply=True)
    )

    # The extracted amount is corrected from the same article → the rebuilt
    # claim drifts from the verified one → the sweep re-queues the fact, with
    # the CURRENT claim and the round id as its fact_ref.
    fr.amount_raised = Decimal("65000000")
    await db.flush()
    [fact] = _round_facts(
        await _collect_stored_text_facts(db, limit=10, for_apply=True)
    )
    assert fact.fact_ref == str(fr.id)
    assert fact.claim == funding_round_claim(
        "Epsilon", Decimal("65000000"), "Series B", None, None
    )
