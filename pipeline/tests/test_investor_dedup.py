"""DB-gated integration tests for investor canonicalization + dedup.

Coverage:
1. canonicalize_investor_name alias pairs: a16z ↔ Andreessen Horowitz,
   GV ↔ Google Ventures, NEA ↔ New Enterprise Associates.
2. merge_investors repoints company_investors to the survivor.
3. merge_investors repoints funding_round_investors to the survivor.
4. merge_investors handles duplicate links (unique-constraint conflict)
   without raising IntegrityError.
5. merge_investors promotes is_lead from loser to survivor for shared links.
6. merge_investors deletes the loser row.
7. merge_investors calls refresh_investor_counts so portfolio_count is correct.
8. run_dedup_investors groups by canonical name and merges duplicates.
9. run_dedup_investors is idempotent: a second run is a no-op.
10. run_dedup_investors classifies known VC firms as type='institutional'.
11. run_dedup_investors purges junk placeholder rows + their links (idempotent).
12. run_dedup_investors classifies individuals as type='angel' but never firms.
13. upsert_investor rejects junk placeholder names at insert time.

The pure-string classification logic (is_junk_investor_name,
is_individual_investor_name, a16z aliasing) is unit-tested DB-free in
test_investor_name.py; here we assert the DB-level behaviour.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import (
    Company,
    CompanyInvestor,
    FundingRound,
    FundingRoundInvestor,
    Investor,
)
from nous.db.upsert import merge_investors
from nous.pipeline.dedup_investors import run_dedup_investors
from nous.util.investor_name import canonicalize_investor_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Unit tests for canonicalize_investor_name (no DB needed, but included here
# for grouping — pytestmark skips them only when DATABASE_URL is unset)
# ---------------------------------------------------------------------------


def test_a16z_and_andreessen_horowitz_same_canonical() -> None:
    """a16z and Andreessen Horowitz must resolve to the same canonical key."""
    assert canonicalize_investor_name("a16z") == canonicalize_investor_name(
        "Andreessen Horowitz"
    )


def test_gv_and_google_ventures_same_canonical() -> None:
    """GV and Google Ventures must resolve to the same canonical key."""
    assert canonicalize_investor_name("GV") == canonicalize_investor_name(
        "Google Ventures"
    )


def test_nea_and_new_enterprise_associates_same_canonical() -> None:
    """NEA and New Enterprise Associates must resolve to the same canonical key."""
    assert canonicalize_investor_name("NEA") == canonicalize_investor_name(
        "New Enterprise Associates"
    )


def test_suffix_stripping_still_works() -> None:
    """Existing suffix-strip logic is not broken by the alias map."""
    assert canonicalize_investor_name("Sequoia Capital") == "sequoia"
    assert canonicalize_investor_name("Lightspeed Venture Partners") == "lightspeed"
    assert canonicalize_investor_name("Founders Fund") == "founders"


def test_unrelated_names_do_not_collide() -> None:
    """Two unrelated firms do NOT alias to the same canonical key."""
    assert canonicalize_investor_name("Sequoia Capital") != canonicalize_investor_name(
        "Andreessen Horowitz"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_investor(suffix: str, *, canonical_override: str | None = None) -> Investor:
    """Build an unsaved Investor with a unique name."""
    name = f"Test Firm {suffix}"
    norm = canonical_override or canonicalize_investor_name(name)
    return Investor(
        name=name,
        name_normalized=norm,
        slug=f"test-firm-{suffix}",
    )


def _make_company(suffix: str) -> Company:
    return Company(
        name=f"TestCo {suffix}",
        slug=f"testco-inv-{suffix}",
        normalized_name=f"testco inv {suffix}",
        hq_country="US",
        # Passes the catalog bar — portfolio_count now counts SHOWN companies
        # only (refresh_investor_counts), and a bare name-only row is a husk.
        description_short=f"TestCo {suffix} does things.",
    )


def _ci(company: Company, investor: Investor, *, is_lead: bool = False) -> CompanyInvestor:
    return CompanyInvestor(
        company_id=company.id,
        investor_id=investor.id,
        source="vc_portfolio",
        is_lead=is_lead,
    )


def _round(company: Company) -> FundingRound:
    return FundingRound(company_id=company.id, round_type="Seed")


def _fri(
    funding_round: FundingRound, investor: Investor, *, is_lead: bool = False
) -> FundingRoundInvestor:
    return FundingRoundInvestor(
        funding_round_id=funding_round.id,
        investor_id=investor.id,
        is_lead=is_lead,
    )


# ---------------------------------------------------------------------------
# merge_investors tests
# ---------------------------------------------------------------------------


async def test_merge_investors_repoints_company_investors(db: AsyncSession) -> None:
    """merge_investors moves company_investors links from loser to survivor."""
    survivor = _make_investor("surv-ci")
    loser = _make_investor("lose-ci")
    company = _make_company("ci-1")
    db.add_all([survivor, loser, company])
    await db.flush()
    db.add(_ci(company, loser))
    await db.flush()
    survivor_id: UUID = survivor.id
    loser_id: UUID = loser.id

    await merge_investors(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    # Loser is gone.
    assert await db.get(Investor, loser_id) is None

    # The company link is now on the survivor.
    result = await db.execute(
        select(CompanyInvestor).where(CompanyInvestor.company_id == company.id)
    )
    links = result.scalars().all()
    assert len(links) == 1
    assert links[0].investor_id == survivor_id


async def test_merge_investors_repoints_funding_round_investors(db: AsyncSession) -> None:
    """merge_investors moves funding_round_investors links from loser to survivor."""
    survivor = _make_investor("surv-fri")
    loser = _make_investor("lose-fri")
    company = _make_company("fri-1")
    db.add_all([survivor, loser, company])
    await db.flush()
    rnd = _round(company)
    db.add(rnd)
    await db.flush()
    db.add(_fri(rnd, loser))
    await db.flush()
    survivor_id: UUID = survivor.id
    loser_id: UUID = loser.id

    await merge_investors(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    assert await db.get(Investor, loser_id) is None

    result = await db.execute(
        select(FundingRoundInvestor).where(
            FundingRoundInvestor.funding_round_id == rnd.id
        )
    )
    links = result.scalars().all()
    assert len(links) == 1
    assert links[0].investor_id == survivor_id


async def test_merge_investors_dedupes_company_investor_conflict(
    db: AsyncSession,
) -> None:
    """When survivor and loser both link the same company, the merge must not
    raise an IntegrityError and a single link remains on the survivor."""
    survivor = _make_investor("surv-dup")
    loser = _make_investor("lose-dup")
    company = _make_company("dup-1")
    db.add_all([survivor, loser, company])
    await db.flush()
    db.add_all([_ci(company, survivor), _ci(company, loser)])
    await db.flush()
    survivor_id: UUID = survivor.id
    loser_id: UUID = loser.id

    await merge_investors(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    assert await db.get(Investor, loser_id) is None
    result = await db.execute(
        select(CompanyInvestor).where(CompanyInvestor.company_id == company.id)
    )
    links = result.scalars().all()
    assert len(links) == 1
    assert links[0].investor_id == survivor_id


async def test_merge_investors_dedupes_fri_conflict(db: AsyncSession) -> None:
    """When survivor and loser are both linked to the same funding round, the
    merge deduplicates without IntegrityError and one FRI link remains."""
    survivor = _make_investor("surv-fridup")
    loser = _make_investor("lose-fridup")
    company = _make_company("fridup-1")
    db.add_all([survivor, loser, company])
    await db.flush()
    rnd = _round(company)
    db.add(rnd)
    await db.flush()
    db.add_all([_fri(rnd, survivor), _fri(rnd, loser)])
    await db.flush()
    survivor_id: UUID = survivor.id
    loser_id: UUID = loser.id

    await merge_investors(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    assert await db.get(Investor, loser_id) is None
    result = await db.execute(
        select(FundingRoundInvestor).where(
            FundingRoundInvestor.funding_round_id == rnd.id
        )
    )
    links = result.scalars().all()
    assert len(links) == 1
    assert links[0].investor_id == survivor_id


async def test_merge_investors_promotes_is_lead_company_investor(
    db: AsyncSession,
) -> None:
    """Sticky is_lead: when loser has is_lead=True for a shared company but
    survivor has is_lead=False, the merge sets survivor's link to is_lead=True."""
    survivor = _make_investor("surv-lead")
    loser = _make_investor("lose-lead")
    company = _make_company("lead-1")
    db.add_all([survivor, loser, company])
    await db.flush()
    db.add(_ci(company, survivor, is_lead=False))
    db.add(_ci(company, loser, is_lead=True))
    await db.flush()
    survivor_id: UUID = survivor.id
    loser_id: UUID = loser.id

    await merge_investors(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    result = await db.execute(
        select(CompanyInvestor).where(CompanyInvestor.company_id == company.id)
    )
    links = result.scalars().all()
    assert len(links) == 1
    assert links[0].investor_id == survivor_id
    assert links[0].is_lead is True


async def test_merge_investors_promotes_is_lead_fri(db: AsyncSession) -> None:
    """Sticky is_lead on FundingRoundInvestor: loser's lead flag survives merge."""
    survivor = _make_investor("surv-frilead")
    loser = _make_investor("lose-frilead")
    company = _make_company("frilead-1")
    db.add_all([survivor, loser, company])
    await db.flush()
    rnd = _round(company)
    db.add(rnd)
    await db.flush()
    db.add(_fri(rnd, survivor, is_lead=False))
    db.add(_fri(rnd, loser, is_lead=True))
    await db.flush()
    survivor_id: UUID = survivor.id
    loser_id: UUID = loser.id

    await merge_investors(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    result = await db.execute(
        select(FundingRoundInvestor).where(
            FundingRoundInvestor.funding_round_id == rnd.id
        )
    )
    links = result.scalars().all()
    assert len(links) == 1
    assert links[0].investor_id == survivor_id
    assert links[0].is_lead is True


async def test_merge_investors_recomputes_portfolio_count(db: AsyncSession) -> None:
    """After merge, survivor.portfolio_count reflects the merged company links."""
    survivor = _make_investor("surv-cnt")
    loser = _make_investor("lose-cnt")
    company_a = _make_company("cnt-a")
    company_b = _make_company("cnt-b")
    db.add_all([survivor, loser, company_a, company_b])
    await db.flush()
    # survivor → company_a; loser → company_b
    db.add(_ci(company_a, survivor))
    db.add(_ci(company_b, loser))
    await db.flush()
    await db.commit()
    survivor_id: UUID = survivor.id
    loser_id: UUID = loser.id

    await merge_investors(db, survivor_id=survivor_id, loser_id=loser_id)
    await db.commit()

    await db.refresh(survivor)
    # Survivor should now link company_a AND company_b → portfolio_count = 2.
    assert survivor.portfolio_count == 2


async def test_merge_investors_rejects_self_merge(db: AsyncSession) -> None:
    """merge_investors raises ValueError when survivor == loser."""
    inv = _make_investor("self-merge")
    db.add(inv)
    await db.flush()
    with pytest.raises(ValueError, match="identical"):
        await merge_investors(db, survivor_id=inv.id, loser_id=inv.id)


# ---------------------------------------------------------------------------
# run_dedup_investors (stage-level) tests
# ---------------------------------------------------------------------------


async def test_dedup_investors_merges_alias_duplicates(db: AsyncSession) -> None:
    """run_dedup_investors collapses investor rows whose canonical names alias
    to the same key (e.g. 'a16z' and 'andreessen horowitz')."""
    # Insert two investors with the canonical keys that the alias map equates.
    a16z_canonical = canonicalize_investor_name("a16z")
    ah_canonical = canonicalize_investor_name("Andreessen Horowitz")
    # These must be equal (that's what the alias test asserts above).
    assert a16z_canonical == ah_canonical

    inv_a = Investor(
        name="a16z",
        name_normalized="a16z",
        slug="a16z-dedup-test",
    )
    inv_b = Investor(
        name="Andreessen Horowitz",
        name_normalized="andreessen horowitz",
        slug="andreessen-horowitz-dedup-test",
    )
    company = _make_company("alias-dedup")
    db.add_all([inv_a, inv_b, company])
    await db.flush()
    # Give one link to each so the survivor is deterministic.
    db.add(_ci(company, inv_a))
    await db.flush()
    await db.commit()
    inv_a_id: UUID = inv_a.id
    inv_b_id: UUID = inv_b.id

    summary = await run_dedup_investors(db)

    assert summary.duplicate_groups >= 1
    assert summary.investors_merged >= 1

    # Exactly one of the two rows should survive.
    a_exists = await db.get(Investor, inv_a_id) is not None
    b_exists = await db.get(Investor, inv_b_id) is not None
    assert a_exists ^ b_exists, "Exactly one of the two aliased investors must survive"


async def test_dedup_investors_idempotent(db: AsyncSession) -> None:
    """A second run of run_dedup_investors is a no-op when no duplicates remain."""
    inv_x = Investor(
        name="Unique Firm X",
        name_normalized="unique firm x",
        slug="unique-firm-x-idem",
    )
    inv_y = Investor(
        name="Unique Firm Y",
        name_normalized="unique firm y",
        slug="unique-firm-y-idem",
    )
    db.add_all([inv_x, inv_y])
    await db.flush()
    await db.commit()

    await run_dedup_investors(db)
    second = await run_dedup_investors(db)

    assert second.investors_merged == 0
    assert second.duplicate_groups == 0


async def test_dedup_investors_classifies_institutional(db: AsyncSession) -> None:
    """run_dedup_investors sets type='institutional' for known VC firm rows."""
    from nous.util.investor_name import canonicalize_investor_name as canon

    # Insert a row whose canonical name matches a known VC firm.
    # "sequoia" is the canonical for "Sequoia Capital".
    seq_canonical = canon("Sequoia Capital")
    inv = Investor(
        name="Sequoia Capital",
        name_normalized=seq_canonical,
        slug="sequoia-type-test",
        type="unknown",
    )
    db.add(inv)
    await db.flush()
    await db.commit()
    inv_id: UUID = inv.id

    await run_dedup_investors(db)

    # Expire the in-memory object so db.get re-fetches from DB (the bulk
    # UPDATE in _classify_institutional bypasses the ORM identity map).
    db.expire_all()
    refreshed = await db.get(Investor, inv_id)
    assert refreshed is not None
    assert refreshed.type == "institutional"


async def test_dedup_investors_does_not_classify_unknown_firms(
    db: AsyncSession,
) -> None:
    """run_dedup_investors does NOT set type='institutional' for unknown firms.

    Uses a firm-shaped name (so it is neither a known institutional firm nor an
    individual) to assert it stays 'unknown'.
    """
    inv = Investor(
        name="Obscure Growth Partners",
        name_normalized="obscure growth",
        slug="obscure-growth-type-test",
        type="unknown",
    )
    db.add(inv)
    await db.flush()
    await db.commit()
    inv_id: UUID = inv.id

    await run_dedup_investors(db)

    db.expire_all()
    refreshed = await db.get(Investor, inv_id)
    assert refreshed is not None
    assert refreshed.type == "unknown"


# ---------------------------------------------------------------------------
# Junk-row purge (dedup-investors step 0)
# ---------------------------------------------------------------------------


async def test_dedup_investors_purges_junk_rows(db: AsyncSession) -> None:
    """run_dedup_investors deletes placeholder rows like 'a group of investors'
    along with their (noise) links, and leaves real investors untouched."""
    junk = Investor(
        name="a group of investors",
        name_normalized=canonicalize_investor_name("a group of investors"),
        slug="a-group-of-investors-junk",
    )
    real = Investor(
        name="Sequoia Capital",
        name_normalized=canonicalize_investor_name("Sequoia Capital"),
        slug="sequoia-purge-test",
    )
    company = _make_company("junk-purge")
    db.add_all([junk, real, company])
    await db.flush()
    # Give the junk row both a company link and a funding-round link so we prove
    # both legs are cleaned up by the cascade-less explicit deletes.
    rnd = _round(company)
    db.add(rnd)
    await db.flush()
    db.add(_ci(company, junk))
    db.add(_fri(rnd, junk))
    db.add(_ci(company, real))
    await db.flush()
    await db.commit()
    junk_id: UUID = junk.id
    real_id: UUID = real.id

    summary = await run_dedup_investors(db)

    assert summary.junk_purged >= 1
    # Junk investor and its links are gone.
    assert await db.get(Investor, junk_id) is None
    ci_rows = (
        await db.execute(
            select(CompanyInvestor).where(CompanyInvestor.investor_id == junk_id)
        )
    ).scalars().all()
    assert ci_rows == []
    fri_rows = (
        await db.execute(
            select(FundingRoundInvestor).where(
                FundingRoundInvestor.investor_id == junk_id
            )
        )
    ).scalars().all()
    assert fri_rows == []
    # The real investor survives.
    assert await db.get(Investor, real_id) is not None


async def test_dedup_investors_purge_is_idempotent(db: AsyncSession) -> None:
    """A second run finds no junk to purge."""
    junk = Investor(
        name="undisclosed",
        name_normalized=canonicalize_investor_name("undisclosed"),
        slug="undisclosed-junk-idem",
    )
    db.add(junk)
    await db.flush()
    await db.commit()

    first = await run_dedup_investors(db)
    second = await run_dedup_investors(db)

    assert first.junk_purged >= 1
    assert second.junk_purged == 0


# ---------------------------------------------------------------------------
# Angel classification (dedup-investors step 2b)
# ---------------------------------------------------------------------------


async def test_dedup_investors_classifies_individual_as_angel(
    db: AsyncSession,
) -> None:
    """An individual-looking name (e.g. 'Jeff Bezos') is classified type='angel'."""
    inv = Investor(
        name="Jeff Bezos",
        name_normalized=canonicalize_investor_name("Jeff Bezos"),
        slug="jeff-bezos-angel-test",
        type="unknown",
    )
    db.add(inv)
    await db.flush()
    await db.commit()
    inv_id: UUID = inv.id

    summary = await run_dedup_investors(db)

    assert summary.angel_classifications >= 1
    db.expire_all()
    refreshed = await db.get(Investor, inv_id)
    assert refreshed is not None
    assert refreshed.type == "angel"


async def test_dedup_investors_does_not_classify_firm_as_angel(
    db: AsyncSession,
) -> None:
    """A surname-pair FIRM name (e.g. 'Draper Fisher') is never tagged angel —
    it stays 'unknown'. 'Draper Fisher' is two alphabetic tokens with no firm
    marker, so only the given-name gate keeps it out of the angel bucket; it is
    deliberately NOT one of the ~13 registry firms (which would go
    institutional)."""
    inv = Investor(
        name="Draper Fisher",
        name_normalized=canonicalize_investor_name("Draper Fisher"),
        slug="draper-fisher-angel-test",
        type="unknown",
    )
    db.add(inv)
    await db.flush()
    await db.commit()
    inv_id: UUID = inv.id

    await run_dedup_investors(db)

    db.expire_all()
    refreshed = await db.get(Investor, inv_id)
    assert refreshed is not None
    assert refreshed.type == "unknown"


async def test_dedup_investors_angel_does_not_override_institutional(
    db: AsyncSession,
) -> None:
    """Angel classification only touches 'unknown' rows; an institutional firm
    is left institutional even if it weren't individual-shaped anyway."""
    # Andreessen Horowitz is a known scraped firm → institutional. Even though
    # its display name is a two-token surname pair, the institutional pass runs
    # first and the angel pass skips non-'unknown' rows.
    inv = Investor(
        name="Andreessen Horowitz",
        name_normalized=canonicalize_investor_name("Andreessen Horowitz"),
        slug="a16z-angel-guard-test",
        type="unknown",
    )
    db.add(inv)
    await db.flush()
    await db.commit()
    inv_id: UUID = inv.id

    await run_dedup_investors(db)

    db.expire_all()
    refreshed = await db.get(Investor, inv_id)
    assert refreshed is not None
    assert refreshed.type == "institutional"


# ---------------------------------------------------------------------------
# upsert_investor rejects junk names at insert time
# ---------------------------------------------------------------------------


async def test_upsert_investor_rejects_junk_name(db: AsyncSession) -> None:
    """upsert_investor raises ValueError for a placeholder name and inserts no
    row — the insert-time half of the junk guard."""
    from nous.db.upsert import upsert_investor

    with pytest.raises(ValueError, match="placeholder"):
        await upsert_investor(db, name="a group of investors")

    # Nothing was inserted.
    rows = (
        await db.execute(
            select(Investor).where(
                Investor.name_normalized
                == canonicalize_investor_name("a group of investors")
            )
        )
    ).scalars().all()
    assert rows == []


async def test_upsert_investor_accepts_real_name(db: AsyncSession) -> None:
    """A real investor name still inserts normally (no false-positive rejection)."""
    from nous.db.upsert import upsert_investor

    inv, created = await upsert_investor(db, name="Founders Fund")
    assert created is True
    assert inv.name == "Founders Fund"
    assert inv.name_normalized == canonicalize_investor_name("Founders Fund")
