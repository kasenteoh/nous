"""Cross-session persistence tests for the data-producing pipeline stages.

The bug these guard against: each pipeline stage runs as its own CLI process
under ``async with AsyncSessionLocal() as session:`` — which does NOT
auto-commit. A stage that ``flush()``es per row but never ``commit()``s has
every write ROLLED BACK on session close, so nothing lands in prod. The
single-shared-session ``db`` fixture cannot catch this: a flush is visible to
its own assertions, so a flush-only stage passes there even though prod loses
the data. (This exact bug emptied the competitors table.)

Each test below uses ``committed_session_factory`` (conftest.py) to open THREE
independent sessions on one isolated connection:

1. session #1 — create the stage's eligible inputs and ``commit()`` them.
2. session #2 — run the stage (mocking its ``complete_json`` if it is an LLM
   stage), then let the ``async with`` close the session, exactly as the CLI
   does. A flush-only stage loses its writes here.
3. session #3 — query by the ids captured in #1 and assert the output rows are
   visible. This is the contract: "writes persist across sessions."

A FAILURE here means the stage flushes without committing — a real prod bug, to
be fixed in the stage, NOT papered over by weakening the test.

Gated on DATABASE_URL like the other DB integration tests.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nous.db.models import Company, CompanyRelationship, Competitor, RawPage
from nous.llm.prompts.company_description import CompanyDescription
from nous.llm.prompts.company_description_long import CompanyLongDescription
from nous.llm.prompts.competitor_analysis import (
    Competitor as CompetitorOut,
)
from nous.llm.prompts.competitor_analysis import (
    CompetitorAnalysis,
)
from nous.pipeline.analyze_competitors import run_analyze_competitors
from nous.pipeline.derive_relationships import run_derive_relationships
from nous.pipeline.enrich_companies import run_enrich_companies
from nous.pipeline.link_competitors import run_link_competitors
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

Factory = async_sessionmaker[AsyncSession]


# ---------------------------------------------------------------------------
# Helpers — distinct slug prefixes keep fixtures from colliding with the live
# (committed-then-rolled-back) connection, and os.urandom keeps reruns clean.
# ---------------------------------------------------------------------------


def _make_company(
    name: str,
    *,
    slug_prefix: str,
    description_long: str | None = "Long desc",
    industry_group: str | None = "SaaS",
) -> Company:
    return Company(
        name=name,
        slug=f"{slug_prefix}-{name.lower().replace(' ', '-')}-{os.urandom(4).hex()}",
        normalized_name=normalize_name(name),
        description_short=f"{name} short.",
        description_long=description_long,
        industry_group=industry_group,
        hq_country="US",
    )


# A page whose visible text clears enrich's _MIN_TEXT_CHARS (200) bar.
_SUBSTANTIAL_PAGE = (
    "<html><body><p>This is a substantial enough page to pass the minimum text "
    "check. The company builds developer tools for API-first teams. Their "
    "platform enables engineers to design, test, and deploy APIs at scale. "
    "Founded in 2021, they serve hundreds of enterprise customers globally. "
    "Their flagship product is a cloud-native API gateway with built-in "
    "observability. The team is distributed across North America and Europe."
    "</p></body></html>"
)


# ---------------------------------------------------------------------------
# analyze-competitors
# ---------------------------------------------------------------------------


async def test_analyze_competitors_persists_across_sessions(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A competitor row written by analyze-competitors must be visible from a
    SEPARATE session after the stage's session closes — i.e. it commits."""
    # Session #1: eligible target + a resolvable rival, committed.
    async with committed_session_factory() as s1:
        target = _make_company("AcpTarget", slug_prefix="persist-acp")
        rival = _make_company(
            "AcpRival",
            slug_prefix="persist-acp",
            description_long=None,  # ineligible for analysis; resolution target only
        )
        s1.add_all([target, rival])
        await s1.commit()
        target_id: UUID = target.id
        rival_id: UUID = rival.id

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        assert schema is CompetitorAnalysis
        return CompetitorAnalysis(
            competitors=[
                CompetitorOut(
                    name="AcpRival",
                    description="AcpRival description.",
                    reasoning="AcpRival reasoning.",
                    rank=1,
                )
            ]
        )

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    # Session #2: run the stage exactly as the CLI does, then close the session.
    async with committed_session_factory() as s2:
        summary = await run_analyze_competitors(s2, limit=10, ttl_days=25)
    assert summary.competitors_written >= 1

    # Session #3: the competitor row must be visible from a fresh session.
    async with committed_session_factory() as s3:
        rows = (
            (
                await s3.execute(
                    select(Competitor).where(Competitor.company_id == target_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 1, (
        "analyze-competitors wrote nothing visible from a separate session — "
        "it flushes without committing (prod would lose the competitor rows)."
    )
    assert rows[0].competitor_name == "AcpRival"
    assert rows[0].competitor_company_id == rival_id


# ---------------------------------------------------------------------------
# enrich-companies
# ---------------------------------------------------------------------------


async def test_enrich_companies_persists_across_sessions(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enrich-companies must COMMIT the description it writes: a separate
    session opened after the stage closes must see description_short set."""
    # Session #1: a company with NULL description and a substantial page.
    async with committed_session_factory() as s1:
        company = Company(
            name="EnrichTarget",
            slug=f"persist-enrich-{os.urandom(4).hex()}",
            normalized_name=normalize_name("EnrichTarget"),
            hq_country="US",
        )
        s1.add(company)
        await s1.flush()
        company_id: UUID = company.id
        s1.add(RawPage(company_id=company_id, url="https://enrich.example/",
                       content=_SUBSTANTIAL_PAGE))
        await s1.commit()

    canned = CompanyDescription(
        description_short="A short description of the company.",
        primary_category="developer tools",
        tags=["open-source", "api-first"],
        website_state="ok",
    )
    canned_long = CompanyLongDescription(
        description_long="A longer description.\n\nWith multiple paragraphs."
    )

    async def _fake_complete_json(prompt: str, schema: type) -> object:
        # Two-call flow: the judge then (page permitting) the describe call.
        if schema is CompanyDescription:
            return canned
        assert schema is CompanyLongDescription
        return canned_long

    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json", _fake_complete_json
    )

    # Session #2: run the stage, then close the session.
    async with committed_session_factory() as s2:
        summary = await run_enrich_companies(s2)
    assert summary.companies_enriched >= 1

    # Session #3: the description must be visible from a fresh session.
    async with committed_session_factory() as s3:
        refreshed = await s3.get(Company, company_id)
    assert refreshed is not None
    assert refreshed.description_short == canned.description_short, (
        "enrich-companies' write is not visible from a separate session — "
        "it flushes without committing (prod would lose the enrichment)."
    )


# ---------------------------------------------------------------------------
# link-competitors (zero-LLM)
# ---------------------------------------------------------------------------


async def test_link_competitors_persists_across_sessions(
    committed_session_factory: Factory,
) -> None:
    """The FK link-competitors fills must be visible from a separate session
    after the stage closes — i.e. the per-row commit actually persists."""
    # Session #1: subject + a clearly matching target + a dangling competitor.
    async with committed_session_factory() as s1:
        subject = Company(
            name="Link Subject Inc.",
            slug=f"persist-link-subject-{os.urandom(4).hex()}",
            normalized_name="linksubject",
            hq_country="US",
        )
        target = Company(
            name="Globex Analytics Inc.",
            slug=f"persist-link-target-{os.urandom(4).hex()}",
            normalized_name="globexanalytics",
            hq_country="US",
        )
        s1.add_all([subject, target])
        await s1.flush()
        target_id: UUID = target.id
        comp = Competitor(
            company_id=subject.id,
            competitor_company_id=None,
            competitor_name="Globex Analytics",  # normalizes to globexanalytics
            rank=1,
            source="llm_inferred",
        )
        s1.add(comp)
        await s1.commit()
        comp_id: UUID = comp.id

    # Session #2: run the stage, then close the session.
    async with committed_session_factory() as s2:
        summary = await run_link_competitors(s2)
    assert summary.linked >= 1

    # Session #3: the resolved FK must be visible from a fresh session.
    async with committed_session_factory() as s3:
        refreshed = await s3.get(Competitor, comp_id)
    assert refreshed is not None
    assert refreshed.competitor_company_id == target_id, (
        "link-competitors' FK write is not visible from a separate session — "
        "it flushes without committing (prod would lose the resolved link)."
    )


# ---------------------------------------------------------------------------
# derive-relationships (zero-LLM)
# ---------------------------------------------------------------------------


async def test_derive_relationships_persists_across_sessions(
    committed_session_factory: Factory,
) -> None:
    """A 'similar' edge derived from shared industry + tags must be visible from
    a separate session after the stage closes — i.e. it commits."""
    # Session #1: two same-industry companies sharing tags, committed.
    async with committed_session_factory() as s1:
        a = Company(
            name="DerA",
            slug=f"persist-der-a-{os.urandom(4).hex()}",
            normalized_name="dera",
            hq_country="US",
            industry_group="devtools-persist",
            primary_category="ci",
            tags=["python", "testing", "ci"],
        )
        b = Company(
            name="DerB",
            slug=f"persist-der-b-{os.urandom(4).hex()}",
            normalized_name="derb",
            hq_country="US",
            industry_group="devtools-persist",
            primary_category="ci",
            tags=["python", "testing"],  # shares python+testing+category with A
        )
        s1.add_all([a, b])
        await s1.commit()
        a_id: UUID = a.id
        b_id: UUID = b.id

    # Session #2: run the stage, then close the session.
    async with committed_session_factory() as s2:
        summary = await run_derive_relationships(s2)
    assert summary.similar_edges >= 2

    # Session #3: at least one similar edge between A and B must be visible.
    async with committed_session_factory() as s3:
        rows = (
            (
                await s3.execute(
                    select(CompanyRelationship).where(
                        CompanyRelationship.company_id.in_([a_id, b_id]),
                        CompanyRelationship.related_company_id.in_([a_id, b_id]),
                        CompanyRelationship.relationship_type == "similar",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 1, (
        "derive-relationships wrote no edge visible from a separate session — "
        "it flushes without committing (prod would lose the relationship graph)."
    )
