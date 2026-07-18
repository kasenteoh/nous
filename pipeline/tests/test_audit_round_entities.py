"""DB tests for audit-round-entities — the $0 entity-corroboration probe.

Seeds the observed prod shapes (a Primary-Wave-class wrong round with body
text, a correct round, a headline-only GN round, a no-text round, an excluded
company) and pins the verdict routing + report shape. Requires DATABASE_URL.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle
from nous.pipeline.audit_round_entities import run_audit_round_entities

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _co(slug: str, description: str, **kw: object) -> Company:
    return Company(
        name=slug.replace("-", " ").title(),
        slug=slug,
        normalized_name=slug.replace("-", " "),
        description_short=description,
        **kw,  # type: ignore[arg-type]
    )


async def test_probe_routes_verdicts_and_reports(db: AsyncSession) -> None:
    wave = _co(
        "wave-probe",
        "Wave Probe is building a mobile money network across Africa with "
        "free deposits, withdrawals and flat-fee transfers.",
    )
    samba = _co(
        "samba-probe",
        "Samba Probe builds AI accelerator chips and inference hardware "
        "systems for enterprise datacenters.",
    )
    husk = _co("husk-probe", "A company with a round but no coverage text.")
    gone = _co(
        "excluded-probe",
        "An excluded company whose rounds must not be audited.",
        exclusion_reason="non_us",
    )
    db.add_all([wave, samba, husk, gone])
    await db.flush()

    wrong_url = "https://music.example.com/primary-wave-probe-2b"
    wrong = FundingRound(
        company_id=wave.id,
        amount_raised=Decimal("2200000000"),
        primary_news_url=wrong_url,
    )
    right = FundingRound(
        company_id=samba.id,
        round_type="Series F",
        amount_raised=Decimal("1000000000"),
        primary_news_url="https://tech.example.com/samba-probe-1b",
    )
    headline_wrong = FundingRound(
        company_id=samba.id,
        round_type="Series X",
        amount_raised=Decimal("136000000"),
        primary_news_url="https://news.google.com/rss/articles/impulse-headline",
    )
    no_text = FundingRound(company_id=husk.id, amount_raised=Decimal("5000000"))
    excluded_round = FundingRound(
        company_id=gone.id, amount_raised=Decimal("99000000")
    )
    db.add_all([wrong, right, headline_wrong, no_text, excluded_round])
    await db.flush()
    db.add_all(
        [
            NewsArticle(
                company_id=wave.id,
                url=wrong_url,
                title="Primary Wave Probe Acquires Catalog Stake",
                source="music.example.com",
                raw_content=(
                    "Primary Wave Probe announced a $2.2 billion raise led by "
                    "Brookfield. The deal makes Primary Wave Probe one of the "
                    "largest music publishers. Primary Wave Probe's catalog "
                    "spans decades."
                ),
                funding_round_id=wrong.id,
            ),
            NewsArticle(
                company_id=samba.id,
                url="https://tech.example.com/samba-probe-1b",
                title="Samba Probe valued at $11B after $1B round",
                source="tech.example.com",
                raw_content=(
                    "Samba Probe builds AI accelerator hardware for datacenter "
                    "inference workloads. Samba Probe will expand its chips "
                    "business with the new funding."
                ),
                funding_round_id=right.id,
            ),
            # GN-host row: raw_content is headline+snippet, and every proper
            # occurrence of the name extends into a different entity.
            NewsArticle(
                company_id=samba.id,
                url="https://news.google.com/rss/articles/impulse-headline",
                title="Samba Probe Dynamics Raises $136M for Cardiac Devices",
                source="news.google.com",
                raw_content="Samba Probe Dynamics Raises $136M for Cardiac Devices",
                funding_round_id=headline_wrong.id,
            ),
        ]
    )
    await db.commit()

    summary = await run_audit_round_entities(db)

    assert summary.rounds_total == 4  # excluded company's round not selected
    assert summary.rounds_checked == 3
    assert summary.unknown_no_text == 1
    assert summary.corroborated == 1
    # The correct samba article shares description vocabulary → strong.
    assert summary.corroborated_strong == 1
    assert summary.corroborated_weak == 0
    assert summary.suspect == 2
    assert summary.body_texts == 2
    assert summary.headline_texts == 1

    by_slug = {s.slug: s for s in summary.suspects}
    assert set(by_slug) == {"wave-probe", "samba-probe"}
    # Sorted by amount desc: the $2.2B wrong round leads.
    assert summary.suspects[0].slug == "wave-probe"
    assert any("Primary Wave Probe" in e for e in summary.suspects[0].evidence)
    assert summary.suspects[0].text_kind == "body"
    # The headline-only case flags as an extension suspect (via the
    # repetition rule when the GN snippet repeats the headline, via the
    # all-occurrences-extended rule when it does not — either way the
    # evidence names the other entity).
    assert by_slug["samba-probe"].text_kind == "headline"
    assert any(
        "extend" in r or "longer entity phrase" in r
        for r in by_slug["samba-probe"].reasons
    )
    assert any(
        "Samba Probe Dynamics" in e for e in by_slug["samba-probe"].evidence
    )
    assert summary.suspects_truncated == 0
    # Reason counts aggregate across suspects.
    assert sum(summary.reason_counts.values()) >= 2


async def test_name_absent_from_text_is_suspect(db: AsyncSession) -> None:
    """A round whose stored coverage text never contains the name under ANY
    variant (the IM8-for-bespoke-labs shape): suspect with the absent reason
    — distinct from the unknown/no-text coverage gap."""
    co = _co(
        "absent-probe",
        "Absent Probe builds reinforcement learning environments for "
        "training reliable autonomous agents.",
    )
    db.add(co)
    await db.flush()
    r = FundingRound(
        company_id=co.id,
        amount_raised=Decimal("1000000000"),
        primary_news_url="https://gn.example.com/im8-1b",
    )
    db.add(r)
    await db.flush()
    db.add(
        NewsArticle(
            company_id=co.id,
            url="https://gn.example.com/im8-1b",
            title="Prenetics Raises $1 Billion For IM8",
            source="news.google.com",
            raw_content=(
                "Prenetics Raises $1 Billion For IM8 - the wellness brand "
                "offers supplement formulations."
            ),
            funding_round_id=r.id,
        )
    )
    await db.commit()

    summary = await run_audit_round_entities(db)
    assert summary.suspect == 1
    assert summary.unknown_no_text == 0
    assert "name absent from stored coverage text" in summary.suspects[0].reasons


async def test_headline_verb_is_not_an_extension(db: AsyncSession) -> None:
    """Review-finding regression: a GN headline like "Acme Probe Plans $50M
    Expansion" (raw_content duplicating the title, so the phrase repeats)
    must NOT flag "Acme Probe Plans" as another entity."""
    co = _co(
        "acme-probe",
        "Acme Probe manufactures industrial robotics arms for warehouse "
        "automation and logistics fulfillment.",
    )
    db.add(co)
    await db.flush()
    r = FundingRound(
        company_id=co.id,
        amount_raised=Decimal("50000000"),
        primary_news_url="https://news.google.com/rss/articles/acme-plans",
    )
    db.add(r)
    await db.flush()
    db.add(
        NewsArticle(
            company_id=co.id,
            url="https://news.google.com/rss/articles/acme-plans",
            title="Acme Probe Plans $50M Warehouse Robotics Expansion",
            source="news.google.com",
            raw_content="Acme Probe Plans $50M Warehouse Robotics Expansion",
            funding_round_id=r.id,
        )
    )
    await db.commit()

    summary = await run_audit_round_entities(db)
    assert summary.suspect == 0
    assert summary.corroborated == 1


async def test_min_amount_filter_and_head_token_spare(db: AsyncSession) -> None:
    genesis = _co(
        "genesis-probe",
        "Genesis Probe uses machine learning models to design small "
        "molecule drugs for biotechnology programs.",
    )
    db.add(genesis)
    await db.flush()
    small = FundingRound(
        company_id=genesis.id,
        round_type="Series B",
        amount_raised=Decimal("200000000"),
        primary_news_url="https://bio.example.com/genesis-200m",
    )
    db.add(small)
    await db.flush()
    # Headline uses only the distinctive head token — the variant ladder must
    # spare it (the "Genesis raises $200M" shape).
    db.add(
        NewsArticle(
            company_id=genesis.id,
            url="https://bio.example.com/genesis-200m",
            title="Genesis raises $200M for drug discovery",
            source="bio.example.com",
            raw_content=(
                "Genesis raised a $200M Series B. Genesis applies machine "
                "learning models to small molecule drug design programs."
            ),
            funding_round_id=small.id,
        )
    )
    await db.commit()

    summary = await run_audit_round_entities(db)
    assert summary.suspect == 0
    assert summary.corroborated == 1

    # min_amount filters the selection itself.
    filtered = await run_audit_round_entities(
        db, min_amount=Decimal("500000000")
    )
    assert filtered.rounds_total == 0
