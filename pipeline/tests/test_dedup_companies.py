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
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    CompanyInvestor,
    Competitor,
    FundingRound,
    NewsArticle,
    RawPage,
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
