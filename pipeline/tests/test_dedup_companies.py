"""DB-gated integration tests for the dedup-companies stage + merge_companies.

Requires DATABASE_URL pointing at a Postgres with pg_trgm + schema at head.

Coverage:
- Exact-domain merge: two rows, same domain, different names → one row; the
  survivor keeps the best fields and child rows are repointed.
- Shared-hosting blocklist: two ``*.myshopify.com`` rows are NOT merged.
- Constraint conflict: survivor + loser both link the same investor → no
  IntegrityError, a single link remains.
- Fuzzy path (LLM mocked): high-confidence → merged; low-confidence → not.
- Idempotency: a second run is a no-op.
- merge_companies direct: FK repoint + null-fill across every child table.
- Merge carry-over of company_snapshots / career_moves (incl. prior_company_id)
  / fact_verifications / company_themes, each with a unique-key collision.
- Domain-pass name corroboration: an uncorroborated shared-domain pair goes to
  the (mocked) LLM gate and is merged only on same_company + high confidence;
  dry-run writes nothing.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    CareerMove,
    Company,
    CompanyInvestor,
    CompanySnapshot,
    CompanyTheme,
    Competitor,
    FactVerification,
    FundingRound,
    NewsArticle,
    RawPage,
    Theme,
)
from nous.db.upsert import merge_companies, upsert_investor
from nous.llm.prompts.company_match import CompanyMatch
from nous.pipeline.dedup_companies import run_dedup_companies
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
    website: str | None = None,
    description_long: str | None = None,
    description_short: str | None = None,
    hq_city: str | None = None,
    hq_state: str | None = None,
    created_at: datetime | None = None,
) -> Company:
    suffix = os.urandom(4).hex()
    company = Company(
        name=name,
        slug=f"{normalize_name(name) or 'company'}-{suffix}",
        normalized_name=normalize_name(name),
        hq_country="US",
        website=website,
        description_long=description_long,
        description_short=description_short,
        hq_city=hq_city,
        hq_state=hq_state,
    )
    if created_at is not None:
        company.created_at = created_at
    return company


async def _count_for_company(
    session: AsyncSession, model: type, company_id: object
) -> int:
    """Count rows of ``model`` whose company_id == ``company_id``."""
    stmt = (
        select(func.count())
        .select_from(model)
        .where(model.company_id == company_id)  # type: ignore[attr-defined]
    )
    return int((await session.execute(stmt)).scalar_one())


# ---------------------------------------------------------------------------
# Exact-domain pass
# ---------------------------------------------------------------------------


async def test_domain_merge_collapses_same_website(db: AsyncSession) -> None:
    """Two rows with the same canonical domain (different names) collapse to
    one, and the survivor is the more-enriched / earlier row."""
    older = _make_company(
        "Acme Robotics",
        website="https://acme.com",
        description_long="Acme builds warehouse robots.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = _make_company(
        "Acme Inc",
        website="https://www.acme.com/home",  # same host, www + path
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    db.add_all([older, newer])
    await db.flush()
    await db.commit()
    older_id, newer_id = older.id, newer.id

    summary = await run_dedup_companies(db, llm_limit=0)

    assert summary.domain_merges == 1
    survivors = (
        (await db.execute(select(Company).where(Company.website.ilike("%acme.com%"))))
        .scalars()
        .all()
    )
    assert len(survivors) == 1
    # Survivor is the one with description_long (older row).
    assert survivors[0].id == older_id
    # Loser is gone.
    assert await db.get(Company, newer_id) is None


async def test_domain_merge_survivor_keeps_best_fields_and_child_rows(
    db: AsyncSession,
) -> None:
    """Survivor inherits the loser's non-null fields it lacked, and the loser's
    child rows (raw_page, funding_round, company_investor) are repointed."""
    # Survivor: has website + description_long but no hq_city.
    survivor = _make_company(
        "Globex",
        website="https://globex.io",
        description_long="Globex makes data tools.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    # Loser: same domain, has hq_city the survivor lacks + child rows.
    loser = _make_company(
        "Globex Corporation",
        website="https://www.globex.io",
        hq_city="Boston",
        hq_state="MA",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    db.add_all([survivor, loser])
    await db.flush()

    db.add(RawPage(company_id=loser.id, url="https://globex.io/about", content="x"))
    db.add(
        FundingRound(
            company_id=loser.id,
            round_type="Seed",
            amount_raised=Decimal("1000000.00"),
            primary_news_url="https://news.example/globex",
        )
    )
    investor, _ = await upsert_investor(db, name=f"Seed Fund {os.urandom(3).hex()}")
    db.add(
        CompanyInvestor(
            company_id=loser.id, investor_id=investor.id, source="vc_portfolio"
        )
    )
    await db.flush()
    await db.commit()
    survivor_id, loser_id = survivor.id, loser.id

    summary = await run_dedup_companies(db, llm_limit=0)
    assert summary.domain_merges == 1

    refreshed = await db.get(Company, survivor_id)
    assert refreshed is not None
    # Null-fill: survivor lacked hq_city, borrowed it from the loser.
    assert refreshed.hq_city == "Boston"
    assert refreshed.hq_state == "MA"
    # Already-set field is untouched.
    assert refreshed.description_long == "Globex makes data tools."

    # Child rows repointed to survivor; none left on the (deleted) loser.
    assert await db.get(Company, loser_id) is None
    pages = (
        (await db.execute(select(RawPage).where(RawPage.url == "https://globex.io/about")))
        .scalars()
        .all()
    )
    assert len(pages) == 1 and pages[0].company_id == survivor_id
    rounds = (
        (await db.execute(select(FundingRound).where(FundingRound.round_type == "Seed")))
        .scalars()
        .all()
    )
    assert len(rounds) == 1 and rounds[0].company_id == survivor_id
    links = (
        (
            await db.execute(
                select(CompanyInvestor).where(
                    CompanyInvestor.investor_id == investor.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(links) == 1 and links[0].company_id == survivor_id


async def test_shared_hosting_not_merged(db: AsyncSession) -> None:
    """Two distinct *.myshopify.com stores are NOT merged — the shared host
    carries no identity signal."""
    a = _make_company("Acme Store", website="https://acme.myshopify.com")
    b = _make_company("Globex Store", website="https://globex.myshopify.com")
    db.add_all([a, b])
    await db.flush()
    await db.commit()
    a_id, b_id = a.id, b.id

    summary = await run_dedup_companies(db, llm_limit=0)

    assert summary.domain_merges == 0
    assert await db.get(Company, a_id) is not None
    assert await db.get(Company, b_id) is not None


async def test_domain_merge_handles_investor_link_conflict(
    db: AsyncSession,
) -> None:
    """Survivor and loser both link the SAME investor → the merge must not raise
    an IntegrityError on the (company_id, investor_id) unique constraint, and a
    single link survives on the survivor."""
    survivor = _make_company(
        "Initech",
        website="https://initech.com",
        description_long="Initech does TPS reports.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    loser = _make_company(
        "Initech Software",
        website="https://www.initech.com",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db.add_all([survivor, loser])
    await db.flush()

    investor, _ = await upsert_investor(db, name=f"Shared VC {os.urandom(3).hex()}")
    db.add_all(
        [
            CompanyInvestor(
                company_id=survivor.id, investor_id=investor.id, source="vc_portfolio"
            ),
            CompanyInvestor(
                company_id=loser.id, investor_id=investor.id, source="news"
            ),
        ]
    )
    await db.flush()
    await db.commit()
    survivor_id = survivor.id

    summary = await run_dedup_companies(db, llm_limit=0)
    assert summary.domain_merges == 1

    links = (
        (
            await db.execute(
                select(CompanyInvestor).where(
                    CompanyInvestor.investor_id == investor.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(links) == 1
    assert links[0].company_id == survivor_id


async def test_merge_promotes_is_lead_from_loser(db: AsyncSession) -> None:
    """Sticky is_lead across a merge: when survivor and loser share an investor
    and only the loser marks it lead, the surviving link inherits is_lead=True."""
    survivor = _make_company(
        "Hooli",
        website="https://hooli.com",
        description_long="Hooli does cloud.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    loser = _make_company(
        "Hooli XYZ",
        website="https://www.hooli.com",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db.add_all([survivor, loser])
    await db.flush()

    investor, _ = await upsert_investor(db, name=f"Lead VC {os.urandom(3).hex()}")
    db.add_all(
        [
            CompanyInvestor(
                company_id=survivor.id,
                investor_id=investor.id,
                source="vc_portfolio",
                is_lead=False,
            ),
            CompanyInvestor(
                company_id=loser.id,
                investor_id=investor.id,
                source="news",
                is_lead=True,
            ),
        ]
    )
    await db.flush()
    survivor_id = survivor.id

    await merge_companies(db, survivor_id=survivor.id, loser_id=loser.id)
    await db.flush()

    links = (
        (
            await db.execute(
                select(CompanyInvestor).where(
                    CompanyInvestor.investor_id == investor.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(links) == 1
    assert links[0].company_id == survivor_id
    assert links[0].is_lead is True


# ---------------------------------------------------------------------------
# Fuzzy pass (LLM mocked)
# ---------------------------------------------------------------------------


async def test_fuzzy_high_confidence_merges(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two similarly-named rows (no shared domain) with a HIGH-confidence LLM
    verdict are merged via the fuzzy path."""
    a = _make_company(
        "Recursive Intelligence",
        website="https://recursive-a.example",
        description_long="Recursive builds AI agents.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    b = _make_company(
        "Recursive Intelligence Labs",
        website="https://recursive-b.example",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db.add_all([a, b])
    await db.flush()
    await db.commit()
    a_id, b_id = a.id, b.id

    async def _fake_complete_json(prompt: str, schema: type) -> CompanyMatch:
        return CompanyMatch(same_company=True, confidence="high")

    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json", _fake_complete_json
    )

    summary = await run_dedup_companies(db, llm_limit=50)
    assert summary.llm_judged >= 1
    assert summary.llm_merges == 1
    # Survivor is the one with description_long (a).
    assert await db.get(Company, a_id) is not None
    assert await db.get(Company, b_id) is None


async def test_fuzzy_low_confidence_not_merged(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A low-confidence verdict (or same_company without high) does NOT merge."""
    a = _make_company(
        "Recursive Intelligence",
        website="https://recursive-a.example",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    b = _make_company(
        "Recursive Intelligence Labs",
        website="https://recursive-b.example",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db.add_all([a, b])
    await db.flush()
    await db.commit()
    a_id, b_id = a.id, b.id

    async def _fake_complete_json(prompt: str, schema: type) -> CompanyMatch:
        return CompanyMatch(same_company=True, confidence="low")

    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json", _fake_complete_json
    )

    summary = await run_dedup_companies(db, llm_limit=50)
    assert summary.llm_judged >= 1
    assert summary.llm_merges == 0
    assert await db.get(Company, a_id) is not None
    assert await db.get(Company, b_id) is not None


async def test_fuzzy_dry_run_does_not_merge(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run counts what would merge but leaves both rows in place."""
    a = _make_company(
        "Recursive Intelligence",
        website="https://recursive-a.example",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    b = _make_company(
        "Recursive Intelligence Labs",
        website="https://recursive-b.example",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db.add_all([a, b])
    await db.flush()
    await db.commit()
    a_id, b_id = a.id, b.id

    async def _fake_complete_json(prompt: str, schema: type) -> CompanyMatch:
        return CompanyMatch(same_company=True, confidence="high")

    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json", _fake_complete_json
    )

    summary = await run_dedup_companies(db, llm_limit=50, dry_run=True)
    assert summary.llm_merges == 1
    # Both rows still present — nothing committed.
    assert await db.get(Company, a_id) is not None
    assert await db.get(Company, b_id) is not None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_second_run_is_noop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a run merges everything mergeable, a second run finds nothing."""
    # One domain cluster + one fuzzy pair.
    d1 = _make_company(
        "Stark Industries",
        website="https://stark.com",
        description_long="Stark makes reactors.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    d2 = _make_company(
        "Stark Inc", website="https://www.stark.com",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    f1 = _make_company(
        "Wayne Enterprises",
        website="https://wayne-a.example",
        description_long="Wayne builds tech.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    f2 = _make_company(
        "Wayne Enterprises Holdings",
        website="https://wayne-b.example",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db.add_all([d1, d2, f1, f2])
    await db.flush()
    await db.commit()

    async def _fake_complete_json(prompt: str, schema: type) -> CompanyMatch:
        return CompanyMatch(same_company=True, confidence="high")

    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json", _fake_complete_json
    )

    first = await run_dedup_companies(db, llm_limit=50)
    assert first.domain_merges == 1
    assert first.llm_merges == 1

    second = await run_dedup_companies(db, llm_limit=50)
    assert second.domain_merges == 0
    assert second.llm_merges == 0


# ---------------------------------------------------------------------------
# merge_companies direct
# ---------------------------------------------------------------------------


async def test_merge_companies_repoints_and_fills(db: AsyncSession) -> None:
    """merge_companies repoints every child FK and fills survivor NULLs."""
    survivor = _make_company("Survivor Co", website="https://survivor.example")
    loser = _make_company(
        "Loser Co",
        description_long="Loser had a long description.",
        hq_city="Denver",
        hq_state="CO",
    )
    db.add_all([survivor, loser])
    await db.flush()

    # Child rows on the loser.
    db.add(RawPage(company_id=loser.id, url="https://loser.example/p", content="c"))
    db.add(NewsArticle(
        company_id=loser.id,
        url=f"https://news.example/{os.urandom(4).hex()}",
        title="t",
        source="techcrunch.com",
        raw_content="body",
    ))
    db.add(FundingRound(company_id=loser.id, round_type="Series A"))
    inv, _ = await upsert_investor(db, name=f"Merge VC {os.urandom(3).hex()}")
    db.add(CompanyInvestor(company_id=loser.id, investor_id=inv.id, source="news"))
    # A competitor ROW owned by the loser (should be deleted), and a competitor
    # row owned by survivor that POINTS at the loser (should be repointed).
    db.add(Competitor(
        company_id=loser.id, competitor_name="Some Rival", rank=1,
    ))
    db.add(Competitor(
        company_id=survivor.id,
        competitor_company_id=loser.id,
        competitor_name="Loser Co",
        rank=1,
    ))
    await db.flush()
    survivor_id, loser_id = survivor.id, loser.id

    await merge_companies(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    # Loser gone.
    assert await db.get(Company, loser_id) is None
    # Null-fill from loser.
    refreshed = await db.get(Company, survivor_id)
    assert refreshed is not None
    assert refreshed.description_long == "Loser had a long description."
    assert refreshed.hq_city == "Denver"
    # Survivor's own website preserved.
    assert refreshed.website == "https://survivor.example"

    # Child FKs repointed.
    assert await _count_for_company(db, RawPage, loser_id) == 0
    assert await _count_for_company(db, RawPage, survivor_id) == 1
    assert await _count_for_company(db, NewsArticle, survivor_id) == 1
    assert await _count_for_company(db, FundingRound, survivor_id) == 1
    assert await _count_for_company(db, CompanyInvestor, survivor_id) == 1

    # Loser's own competitor row deleted; the survivor's pointer-row was
    # repointed to survivor and then dropped as a self-reference.
    comps = (
        (await db.execute(select(Competitor).where(Competitor.company_id == survivor_id)))
        .scalars()
        .all()
    )
    assert all(c.competitor_company_id != loser_id for c in comps)
    assert all(c.competitor_company_id != survivor_id for c in comps)


async def test_merge_companies_competitor_pointer_dedup(db: AsyncSession) -> None:
    """When survivor and loser are BOTH referenced as a competitor by a third
    company, repointing collapses them — only one (company, competitor) row
    remains, no unique-constraint violation."""
    survivor = _make_company("Surv", website="https://surv.example")
    loser = _make_company("Lose", website="https://lose.example")
    third = _make_company("Third Party")
    db.add_all([survivor, loser, third])
    await db.flush()

    # Third lists BOTH survivor and loser as competitors (ranks 1 and 2).
    db.add(Competitor(
        company_id=third.id, competitor_company_id=survivor.id,
        competitor_name="Surv", rank=1,
    ))
    db.add(Competitor(
        company_id=third.id, competitor_company_id=loser.id,
        competitor_name="Lose", rank=2,
    ))
    await db.flush()
    survivor_id, loser_id, third_id = survivor.id, loser.id, third.id

    await merge_companies(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    rows = (
        (
            await db.execute(
                select(Competitor).where(Competitor.company_id == third_id)
            )
        )
        .scalars()
        .all()
    )
    # The two pointers collapsed into one (both now point at survivor).
    pointing_at_survivor = [
        r for r in rows if r.competitor_company_id == survivor_id
    ]
    assert len(pointing_at_survivor) == 1


async def test_merge_companies_rejects_self_merge(db: AsyncSession) -> None:
    survivor = _make_company("Self", website="https://self.example")
    db.add(survivor)
    await db.flush()
    with pytest.raises(ValueError, match="identical"):
        await merge_companies(db, survivor_id=survivor.id, loser_id=survivor.id)


async def test_merge_companies_raw_page_url_conflict(db: AsyncSession) -> None:
    """When survivor and loser both have a raw_page at the same url, the merge
    keeps the survivor's and drops the loser's — no (company_id, url) violation."""
    survivor = _make_company("S", website="https://s.example")
    loser = _make_company("L", website="https://l.example")
    db.add_all([survivor, loser])
    await db.flush()
    shared_url = "https://shared.example/home"
    db.add(RawPage(company_id=survivor.id, url=shared_url, content="survivor"))
    db.add(RawPage(company_id=loser.id, url=shared_url, content="loser"))
    # Plus a loser-only url that should move over.
    db.add(RawPage(company_id=loser.id, url="https://l.example/only", content="x"))
    await db.flush()
    survivor_id, loser_id = survivor.id, loser.id

    await merge_companies(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    pages = (
        (await db.execute(select(RawPage).where(RawPage.company_id == survivor_id)))
        .scalars()
        .all()
    )
    urls = sorted(p.url for p in pages)
    assert urls == ["https://l.example/only", shared_url]
    # The kept shared row is the survivor's content, not the loser's.
    shared_row = next(p for p in pages if p.url == shared_url)
    assert shared_row.content == "survivor"


def test_prompt_dict_carries_latest_funding() -> None:
    from datetime import date as _date
    from datetime import datetime as _dt
    from decimal import Decimal as _Dec
    from uuid import uuid4

    from nous.pipeline.dedup_companies import _CompanyRow

    row = _CompanyRow(
        id=uuid4(),
        name="Bunkerhill",
        normalized_name="bunkerhill",
        website=None,
        hq_city=None,
        hq_state=None,
        description_short=None,
        description_long=None,
        latest_round_amount=_Dec("55000000"),
        latest_round_date=_date(2026, 7, 10),
        latest_round_type="Series B",
        created_at=_dt(2026, 1, 1),
    )
    d = row.to_prompt_dict()
    assert d["latest_funding"] == "Series B $55,000,000 announced 2026-07-10"

    bare = _CompanyRow(
        id=uuid4(),
        name="X",
        normalized_name="x",
        website=None,
        hq_city=None,
        hq_state=None,
        description_short=None,
        description_long=None,
        latest_round_amount=None,
        latest_round_date=None,
        latest_round_type=None,
        created_at=_dt(2026, 1, 1),
    )
    assert bare.to_prompt_dict()["latest_funding"] is None


# ---------------------------------------------------------------------------
# merge_companies: child tables that used to CASCADE away with the loser
# ---------------------------------------------------------------------------


async def test_merge_carries_company_snapshots(db: AsyncSession) -> None:
    """Loser-only weeks move over; a week both have folds into the survivor's
    row (news summed — disjoint article sets; headcount survivor-first, else
    the loser's pair)."""
    survivor = _make_company("Snap Survivor", website="https://snap-s.example")
    loser = _make_company("Snap Loser")
    db.add_all([survivor, loser])
    await db.flush()
    shared_no_hc = date(2026, 6, 1)  # survivor has no headcount that week
    shared_with_hc = date(2026, 6, 8)  # both have headcount
    loser_only = date(2026, 6, 15)
    survivor_only = date(2026, 6, 22)
    db.add_all(
        [
            CompanySnapshot(
                company_id=survivor.id, captured_week=shared_no_hc, news_count_30d=3
            ),
            CompanySnapshot(
                company_id=loser.id,
                captured_week=shared_no_hc,
                news_count_30d=2,
                employee_count_min=11,
                employee_count_max=50,
            ),
            CompanySnapshot(
                company_id=survivor.id,
                captured_week=shared_with_hc,
                news_count_30d=4,
                employee_count_min=1,
                employee_count_max=10,
            ),
            CompanySnapshot(
                company_id=loser.id,
                captured_week=shared_with_hc,
                news_count_30d=1,
                employee_count_min=11,
                employee_count_max=50,
            ),
            CompanySnapshot(
                company_id=loser.id,
                captured_week=loser_only,
                news_count_30d=5,
                employee_count_min=51,
                employee_count_max=200,
            ),
            CompanySnapshot(
                company_id=survivor.id, captured_week=survivor_only, news_count_30d=7
            ),
        ]
    )
    await db.flush()
    survivor_id, loser_id = survivor.id, loser.id

    await merge_companies(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    rows = (
        await db.execute(
            select(
                CompanySnapshot.captured_week,
                CompanySnapshot.news_count_30d,
                CompanySnapshot.employee_count_min,
                CompanySnapshot.employee_count_max,
            )
            .where(CompanySnapshot.company_id == survivor_id)
            .order_by(CompanySnapshot.captured_week)
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        (shared_no_hc, 5, 11, 50),
        (shared_with_hc, 5, 1, 10),
        (loser_only, 5, 51, 200),
        (survivor_only, 7, None, None),
    ]
    assert await _count_for_company(db, CompanySnapshot, loser_id) == 0


async def test_merge_carries_career_moves_and_repoints_prior_company(
    db: AsyncSession,
) -> None:
    """Colliding (person, prior) edges keep the survivor's row; the rest move
    over; a third company's edge pointing at the loser now points at the
    survivor; an edge that would point the merged company at itself is
    unlinked (the biographical fact is kept)."""
    survivor = _make_company("Career Survivor", website="https://career-s.example")
    loser = _make_company("Career Loser")
    third = _make_company("Career Third")
    db.add_all([survivor, loser, third])
    await db.flush()
    version = "2026-07-01.1"

    def _move(
        company_id: object,
        person: str,
        prior: str,
        *,
        prior_company_id: object = None,
        role: str | None = None,
    ) -> CareerMove:
        return CareerMove(
            company_id=company_id,
            person_name=person,
            person_normalized_name=normalize_name(person),
            prior_company_name=prior,
            prior_company_id=prior_company_id,
            prior_role=role,
            extraction_prompt_version=version,
        )

    db.add_all(
        [
            _move(survivor.id, "Ada Lovelace", "Google", role="Survivor role"),
            # Collides with the survivor's edge → dropped.
            _move(loser.id, "Ada Lovelace", "Google", role="Loser role"),
            # Survivor lacks it → carried.
            _move(loser.id, "Grace Hopper", "Microsoft"),
            # Loser's founder "previously at" the survivor → self-edge, unlinked.
            _move(
                loser.id,
                "Edsger Dijkstra",
                "Career Survivor",
                prior_company_id=survivor.id,
            ),
            # A third company's founder came from the loser → repointed.
            _move(third.id, "Alan Turing", "Career Loser", prior_company_id=loser.id),
        ]
    )
    await db.flush()
    survivor_id, loser_id, third_id = survivor.id, loser.id, third.id

    await merge_companies(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    survivor_moves = (
        await db.execute(
            select(
                CareerMove.person_name,
                CareerMove.prior_company_name,
                CareerMove.prior_role,
                CareerMove.prior_company_id,
            ).where(CareerMove.company_id == survivor_id)
        )
    ).all()
    assert sorted(tuple(r) for r in survivor_moves) == [
        ("Ada Lovelace", "Google", "Survivor role", None),
        ("Edsger Dijkstra", "Career Survivor", None, None),
        ("Grace Hopper", "Microsoft", None, None),
    ]
    third_prior = (
        await db.execute(
            select(CareerMove.prior_company_id).where(
                CareerMove.company_id == third_id
            )
        )
    ).scalar_one()
    assert third_prior == survivor_id


async def test_merge_carries_fact_verifications(db: AsyncSession) -> None:
    """Survivor wins a colliding (fact_kind, fact_ref); the loser's other
    verdicts move over, and a funding-round verdict stays keyed to its round
    (which keeps its id when repointed)."""
    survivor = _make_company("Verify Survivor", website="https://verify-s.example")
    loser = _make_company("Verify Loser")
    db.add_all([survivor, loser])
    await db.flush()
    loser_round = FundingRound(company_id=loser.id, round_type="Seed")
    db.add(loser_round)
    await db.flush()
    round_ref = str(loser_round.id)

    def _verdict(
        company_id: object, kind: str, ref: str, verdict: str, claim: str
    ) -> FactVerification:
        return FactVerification(
            company_id=company_id,
            fact_kind=kind,
            fact_ref=ref,
            source_url="https://news.example/a",
            claim=claim,
            verdict=verdict,
            prompt_version="2026-07-01.1",
        )

    db.add_all(
        [
            _verdict(survivor.id, "total_raised", "", "supported", "survivor total"),
            _verdict(loser.id, "total_raised", "", "unsupported", "loser total"),
            _verdict(loser.id, "status", "", "supported", "loser status"),
            _verdict(loser.id, "funding_round", round_ref, "supported", "loser round"),
        ]
    )
    await db.flush()
    survivor_id, loser_id, round_id = survivor.id, loser.id, loser_round.id

    await merge_companies(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    rows = (
        await db.execute(
            select(
                FactVerification.fact_kind,
                FactVerification.fact_ref,
                FactVerification.verdict,
                FactVerification.claim,
            ).where(FactVerification.company_id == survivor_id)
        )
    ).all()
    assert sorted(tuple(r) for r in rows) == [
        ("funding_round", round_ref, "supported", "loser round"),
        ("status", "", "supported", "loser status"),
        ("total_raised", "", "supported", "survivor total"),
    ]
    round_owner = (
        await db.execute(
            select(FundingRound.company_id).where(FundingRound.id == round_id)
        )
    ).scalar_one()
    assert round_owner == survivor_id


def _make_theme(slug_stem: str, company_count: int) -> Theme:
    return Theme(
        slug=f"{slug_stem}-{os.urandom(4).hex()}",
        name=slug_stem.title(),
        industry_group="developer-tools",
        centroid=[0.0] * 384,
        company_count=company_count,
    )


async def test_merge_company_themes_survivor_membership_wins(
    db: AsyncSession,
) -> None:
    """Both halves in the same theme → one membership (the survivor's) and the
    theme's denormalized company_count drops by one."""
    survivor = _make_company("Theme Survivor", website="https://theme-s.example")
    loser = _make_company("Theme Loser")
    theme = _make_theme("shared-theme", company_count=2)
    db.add_all([survivor, loser, theme])
    await db.flush()
    db.add_all(
        [
            CompanyTheme(theme_id=theme.id, company_id=survivor.id, similarity=0.9),
            CompanyTheme(theme_id=theme.id, company_id=loser.id, similarity=0.8),
        ]
    )
    await db.flush()
    survivor_id, loser_id, theme_id = survivor.id, loser.id, theme.id

    await merge_companies(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    members = (
        await db.execute(
            select(CompanyTheme.company_id, CompanyTheme.similarity).where(
                CompanyTheme.theme_id == theme_id
            )
        )
    ).all()
    assert [tuple(m) for m in members] == [(survivor_id, 0.9)]
    count = (
        await db.execute(select(Theme.company_count).where(Theme.id == theme_id))
    ).scalar_one()
    assert count == 1


async def test_merge_company_themes_adopted_when_survivor_has_none(
    db: AsyncSession,
) -> None:
    survivor = _make_company("Theme Adopter", website="https://theme-a.example")
    loser = _make_company("Theme Donor")
    theme = _make_theme("donor-theme", company_count=1)
    db.add_all([survivor, loser, theme])
    await db.flush()
    db.add(CompanyTheme(theme_id=theme.id, company_id=loser.id, similarity=0.7))
    await db.flush()
    survivor_id, loser_id, theme_id = survivor.id, loser.id, theme.id

    await merge_companies(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    members = (
        (
            await db.execute(
                select(CompanyTheme.company_id).where(
                    CompanyTheme.theme_id == theme_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert list(members) == [survivor_id]
    count = (
        await db.execute(select(Theme.company_count).where(Theme.id == theme_id))
    ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# Domain pass: name corroboration gates the auto-merge
# ---------------------------------------------------------------------------


def _llm_returning(
    calls: list[str], *, same_company: bool, confidence: str
) -> Callable[[str, type], Awaitable[CompanyMatch]]:
    """A complete_json stand-in that records each prompt and returns a fixed
    verdict."""

    async def _fake_complete_json(prompt: str, schema: type) -> CompanyMatch:
        calls.append(prompt)
        return CompanyMatch.model_validate(
            {"same_company": same_company, "confidence": confidence}
        )

    return _fake_complete_json


async def test_domain_corroborated_names_auto_merge_without_llm(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    survivor = _make_company(
        "Acme Robotics",
        website="https://acme.com",
        description_long="Acme builds warehouse robots.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    loser = _make_company(
        "Acme Inc",
        website="https://www.acme.com/home",
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    db.add_all([survivor, loser])
    await db.commit()
    survivor_id, loser_id = survivor.id, loser.id
    calls: list[str] = []
    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json",
        _llm_returning(calls, same_company=False, confidence="low"),
    )

    summary = await run_dedup_companies(db, llm_limit=50)

    assert summary.domain_merges == 1
    assert summary.domain_merges_name_corroborated == 1
    assert summary.domain_pairs_sent_to_llm == 0
    assert calls == []
    assert await db.get(Company, survivor_id) is not None
    assert await db.get(Company, loser_id) is None


@pytest.mark.parametrize(
    ("same_company", "confidence"),
    [(False, "high"), (True, "medium"), (True, "low")],
)
async def test_domain_uncorroborated_pair_goes_to_llm_and_is_not_merged(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    same_company: bool,
    confidence: str,
) -> None:
    """A Kalshi row carrying FrenFlow's website is NOT auto-merged: the pair is
    judged by the LLM gate and survives anything short of same + high."""
    kalshi = _make_company(
        "Kalshi",
        website="https://frenflow.com/",
        description_long="Kalshi runs a regulated prediction market.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    frenflow = _make_company(
        "FrenFlow",
        website="https://www.frenflow.com",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db.add_all([kalshi, frenflow])
    await db.commit()
    kalshi_id, frenflow_id = kalshi.id, frenflow.id
    calls: list[str] = []
    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json",
        _llm_returning(calls, same_company=same_company, confidence=confidence),
    )

    summary = await run_dedup_companies(db, llm_limit=50)

    assert len(calls) == 1
    assert "Kalshi" in calls[0] and "FrenFlow" in calls[0]
    assert summary.domain_merges == 0
    assert summary.domain_merges_name_corroborated == 0
    assert summary.domain_pairs_sent_to_llm == 1
    assert summary.domain_pairs_rejected == 1
    assert summary.domain_pairs_llm_merged == 0
    assert summary.llm_merges == 0
    assert await db.get(Company, kalshi_id) is not None
    assert await db.get(Company, frenflow_id) is not None


async def test_domain_uncorroborated_pair_merges_on_high_confidence(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM can still confirm a shared-domain pair whose names don't spell
    the domain (a rebrand); it counts as a domain merge, not a fuzzy one."""
    old_name = _make_company(
        "Facebook",
        website="https://meta.com",
        description_long="Social networking.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    new_name = _make_company(
        "Zuck Platforms",
        website="https://www.meta.com",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db.add_all([old_name, new_name])
    await db.commit()
    survivor_id, loser_id = old_name.id, new_name.id
    calls: list[str] = []
    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json",
        _llm_returning(calls, same_company=True, confidence="high"),
    )

    summary = await run_dedup_companies(db, llm_limit=50)

    assert len(calls) == 1
    assert summary.domain_merges == 1
    assert summary.domain_pairs_sent_to_llm == 1
    assert summary.domain_pairs_llm_merged == 1
    assert summary.llm_merges == 0
    assert await db.get(Company, survivor_id) is not None
    assert await db.get(Company, loser_id) is None


async def test_domain_uncorroborated_pair_deferred_without_llm_budget(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    kalshi = _make_company("Kalshi", website="https://frenflow.com")
    frenflow = _make_company("FrenFlow", website="https://frenflow.com")
    db.add_all([kalshi, frenflow])
    await db.commit()
    kalshi_id, frenflow_id = kalshi.id, frenflow.id
    calls: list[str] = []
    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json",
        _llm_returning(calls, same_company=True, confidence="high"),
    )

    summary = await run_dedup_companies(db, llm_limit=0)

    assert calls == []
    assert summary.skipped == 1
    assert summary.domain_merges == 0
    assert await db.get(Company, kalshi_id) is not None
    assert await db.get(Company, frenflow_id) is not None


async def test_domain_mixed_cluster_merges_only_corroborated_members(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FrenFlow + FrenFlow Inc collapse; the intruder Kalshi — the most-enriched
    row, which the old rule would have made everyone's survivor — is judged by
    the LLM against FrenFlow's survivor and left alone on a decline."""
    kalshi = _make_company(
        "Kalshi",
        website="https://frenflow.com",
        description_long="Prediction markets.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    frenflow = _make_company(
        "FrenFlow",
        website="https://frenflow.com/about",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    frenflow_inc = _make_company(
        "FrenFlow Inc",
        website="https://www.frenflow.com",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    db.add_all([kalshi, frenflow, frenflow_inc])
    await db.commit()
    kalshi_id, frenflow_id, frenflow_inc_id = kalshi.id, frenflow.id, frenflow_inc.id
    calls: list[str] = []
    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json",
        _llm_returning(calls, same_company=False, confidence="low"),
    )

    summary = await run_dedup_companies(db, llm_limit=50)

    assert summary.domain_merges == 1
    assert summary.domain_merges_name_corroborated == 1
    assert summary.domain_pairs_sent_to_llm == 1
    assert summary.domain_pairs_rejected == 1
    assert await db.get(Company, kalshi_id) is not None
    assert await db.get(Company, frenflow_id) is not None
    assert await db.get(Company, frenflow_inc_id) is None


async def test_domain_pass_dry_run_writes_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run reports the corroborated merge AND the LLM-confirmed domain
    merge it would make, but every row is still there afterwards."""
    acme = _make_company(
        "Acme Robotics",
        website="https://acme.com",
        description_long="Robots.",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    acme_inc = _make_company(
        "Acme Inc",
        website="https://www.acme.com",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    kalshi = _make_company("Kalshi", website="https://frenflow.com")
    frenflow = _make_company("FrenFlow", website="https://frenflow.com")
    db.add_all([acme, acme_inc, kalshi, frenflow])
    await db.commit()
    ids = [acme.id, acme_inc.id, kalshi.id, frenflow.id]
    companies_before = (
        await db.execute(select(func.count()).select_from(Company))
    ).scalar_one()
    calls: list[str] = []
    monkeypatch.setattr(
        "nous.pipeline.dedup_companies.complete_json",
        _llm_returning(calls, same_company=True, confidence="high"),
    )

    summary = await run_dedup_companies(db, llm_limit=50, dry_run=True)

    assert summary.domain_merges == 2
    assert summary.domain_merges_name_corroborated == 1
    assert summary.domain_pairs_llm_merged == 1
    companies_after = (
        await db.execute(select(func.count()).select_from(Company))
    ).scalar_one()
    assert companies_after == companies_before
    for company_id in ids:
        assert await db.get(Company, company_id) is not None
