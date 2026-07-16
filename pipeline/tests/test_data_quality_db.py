"""DB-gated integration tests for the data-quality completeness report."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification, Person
from nous.pipeline.data_quality import run_data_quality

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _company(name: str, *, normalized: str | None = None, **kwargs: object) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{name.lower()}-{suffix}",
        normalized_name=normalized or f"{name.lower()}{suffix}",
        **kwargs,  # type: ignore[arg-type]
    )


async def test_completeness_report(db: AsyncSession) -> None:
    now = datetime.now(UTC)
    # A: fully complete + a person.
    a = _company(
        "Alpha",
        website="https://alpha.com/",
        website_source="news_outbound",
        description_short="Alpha does things.",
        funding_round_count=2,
        hq_country="US",
        industry_group="AI",
        logo_url="https://alpha.com/logo.png",
        tags=["ai"],
        employee_count_min=10,
        last_enriched_at=now,
    )
    # B: bare husk (name only), never enriched.
    b = _company("Bravo")
    # C + E: share a normalized_name (a duplicate group).
    c = _company(
        "Charlie",
        normalized="dupco",
        website="https://charlie.com/",
        website_source="wikidata",
        description_short="Charlie profile.",
        last_enriched_at=now - timedelta(days=60),
    )
    e = _company(
        "CharlieToo",
        normalized="dupco",
        website="https://charlie-two.com/",  # legacy: no website_source
        last_enriched_at=now - timedelta(days=120),
    )
    # D: excluded — must not be counted.
    d = _company(
        "Delta",
        website="https://delta.com/",
        description_short="x",
        exclusion_reason="non_us",
    )
    for co in (a, b, c, e, d):
        db.add(co)
    await db.flush()
    db.add(Person(company_id=a.id, name="Ada", role="CEO", rank=1))
    await db.commit()

    summary = await run_data_quality(db)

    assert summary.shown_total == 4  # A, B, C, E — D excluded
    fields = {f.field: f for f in summary.fields}
    assert fields["has_website"].present == 3  # A, C, E
    assert fields["has_description"].present == 2  # A, C
    assert fields["has_funding"].present == 1  # A
    assert fields["has_people"].present == 1  # A

    assert summary.fully_complete == 1  # A (score 1.0)
    assert summary.husks == 2  # B (0.0), E (website-only 0.20)
    assert summary.mean_completeness == pytest.approx(0.40, abs=1e-6)  # (1+0+.4+.2)/4

    assert summary.website_source_counts == {
        "news_outbound": 1,
        "wikidata": 1,
        "unattributed": 1,
    }
    assert summary.duplicate_groups == 1  # dupco
    assert summary.companies_in_duplicates == 2  # C, E
    assert summary.staleness["never enriched"] == 1  # B
    assert summary.staleness["< 30d"] == 1  # A
    assert summary.staleness["30–90d"] == 1  # C
    assert summary.staleness["> 90d"] == 1  # E

    # No fact_verifications seeded → the verification section reports empty.
    assert summary.verification_counts == {}
    assert summary.unsupported_facts == []


async def test_verification_signal_in_report(db: AsyncSession) -> None:
    """fact_verifications verdicts are counted, and the `unsupported` ones are
    itemized (slug + checked claim + source host) — the internal signal for a
    figure its cited source contradicts."""
    co = _company("Verico", description_short="Shown company.")
    db.add(co)
    await db.flush()

    def _fv(kind: str, ref: str, verdict: str, claim: str) -> FactVerification:
        return FactVerification(
            company_id=co.id,
            fact_kind=kind,
            fact_ref=ref,
            source_url="https://news.example.com/verico-round",
            claim=claim,
            verdict=verdict,
            supporting_quote="a quote" if verdict == "supported" else None,
            prompt_version="2026-07-14.1",
        )

    db.add(_fv("total_raised", "", "supported", "Verico has raised $10.0M."))
    db.add(_fv("funding_round", "ref-1", "unsupported", "Verico raised $99.0M."))
    db.add(_fv("funding_round", "ref-2", "uncertain", "Verico raised $5.0M."))
    await db.commit()

    summary = await run_data_quality(db)

    assert summary.verification_counts == {
        "supported": 1,
        "unsupported": 1,
        "uncertain": 1,
    }
    [unsupported] = summary.unsupported_facts
    assert unsupported.slug == co.slug
    assert unsupported.fact_kind == "funding_round"
    assert unsupported.claim == "Verico raised $99.0M."
    assert unsupported.source_host == "news.example.com"
