"""Integration tests for the repair-wrong-websites stage.

Covers:
- (a) aggregator URL reset
- (b) for-sale / parked description reset
- (c) false-exclusion re-queue (personal homepage / wrong site)
- False-positive safety: real e-commerce copy must NOT trigger pass (b)
- Idempotency: second run finds nothing to repair
- dry_run: counts changes but writes nothing

Requires DATABASE_URL (skipped otherwise).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle, RawPage
from nous.pipeline.repair_wrong_websites import run_repair_wrong_websites

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _co(name: str, slug: str, **kw: object) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        **kw,
    )


# ── Pass (a): aggregator URL ─────────────────────────────────────────────────


async def test_aggregator_url_cleared(db: AsyncSession) -> None:
    """A company whose website points to crunchbase.com is reset."""
    co = _co(
        "Lightning AI",
        "lightning-ai",
        website="https://www.crunchbase.com/organization/lightning-ai",
        description_short="An AI platform company.",
        description_long="Long description.",
        website_resolved_at=None,
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)

    assert summary.aggregator_url_reset == 1
    assert summary.parked_desc_reset == 0

    await db.refresh(co)
    assert co.website is None
    assert co.description_short is None
    assert co.description_long is None
    assert co.website_resolved_at is None
    assert co.last_enriched_at is None
    assert "https://www.crunchbase.com/organization/lightning-ai" in co.rejected_urls


async def test_aggregator_url_drops_raw_pages(db: AsyncSession) -> None:
    """raw_pages for the affected company are deleted."""
    co = _co(
        "Soluto",
        "soluto",
        website="https://linkedin.com/company/soluto",
        description_short="Soluto is an IT support platform.",
    )
    db.add(co)
    await db.flush()
    db.add(
        RawPage(
            company_id=co.id,
            url="https://linkedin.com/company/soluto",
            content="LinkedIn company page",
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.aggregator_url_reset == 1

    pages = (
        (await db.execute(select(RawPage).where(RawPage.company_id == co.id)))
        .scalars()
        .all()
    )
    assert pages == []


async def test_bad_url_not_duplicated_in_rejected_urls(db: AsyncSession) -> None:
    """Running twice does not append the same URL to rejected_urls twice."""
    bad_url = "https://theorg.com/org/acme-test"
    co = _co(
        "Acme Test",
        "acme-test-rww",
        website=bad_url,
        description_short="Some description.",
    )
    db.add(co)
    await db.commit()

    # First run clears website; second run must not re-append
    await run_repair_wrong_websites(db)
    await db.refresh(co)
    first_rejected = list(co.rejected_urls)

    # Manually restore website to simulate a hypothetical double-run edge-case
    # (in reality website is now NULL, so the row won't be re-selected — this
    # just verifies the de-dup logic in _reset_website_fields is correct).
    co.website = bad_url
    db.add(co)
    await db.commit()

    await run_repair_wrong_websites(db)
    await db.refresh(co)
    assert co.rejected_urls.count(bad_url) == 1  # still exactly one entry
    _ = first_rejected  # used above


async def test_good_website_not_touched(db: AsyncSession) -> None:
    """A company with a real, non-aggregator website is left untouched."""
    co = _co(
        "Fal AI",
        "fal-ai",
        website="https://fal.ai",
        description_short="Serverless inference for generative AI.",
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.aggregator_url_reset == 0
    await db.refresh(co)
    assert co.website == "https://fal.ai"
    assert co.description_short == "Serverless inference for generative AI."


# ── Pass (b): for-sale / parked description ──────────────────────────────────


async def test_parked_description_cleared(db: AsyncSession) -> None:
    """A company whose description mentions 'domain for sale' gets reset."""
    co = _co(
        "Foodology",
        "foodology",
        website="https://foodology.com",
        description_short=(
            "The domain foodology.com is for sale. "
            "Contact us to purchase this domain."
        ),
        description_long="Parked page.",
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.parked_desc_reset == 1

    await db.refresh(co)
    assert co.website is None
    assert co.description_short is None
    assert co.description_long is None
    assert "https://foodology.com" in co.rejected_urls


async def test_parked_description_buy_this_domain(db: AsyncSession) -> None:
    """'buy this domain' in description triggers pass (b)."""
    co = _co(
        "Some Parked Co",
        "some-parked-co",
        website="https://example.com",
        description_short="Buy this domain today and make it yours.",
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.parked_desc_reset == 1

    await db.refresh(co)
    assert co.website is None
    assert co.description_short is None


async def test_ecommerce_copy_not_flagged(db: AsyncSession) -> None:
    """Real e-commerce product copy must NOT trigger pass (b).

    Regression guard: 'list items for sale' / 'marketplace' without domain
    wording should not be treated as a parked-domain description.
    """
    co = _co(
        "SellRaze",
        "sellraze-rww",
        website="https://sellraze.com",
        description_short=(
            "SellRaze lets sellers list items for sale across marketplaces "
            "using image recognition."
        ),
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.parked_desc_reset == 0
    await db.refresh(co)
    assert co.description_short is not None
    assert co.website == "https://sellraze.com"


async def test_marketplace_brand_alone_not_flagged(db: AsyncSession) -> None:
    """'godaddy' in copy without sale-intent prose must not trip the filter."""
    co = _co(
        "GoDaddy Partner",
        "godaddy-partner-rww",
        website="https://real-partner.com",
        description_short=(
            "A DNS service powered by GoDaddy's infrastructure with enterprise SLAs."
        ),
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.parked_desc_reset == 0
    await db.refresh(co)
    assert co.website == "https://real-partner.com"


# ── Pass (c): false exclusions ────────────────────────────────────────────────


async def test_personal_homepage_exclusion_requeued(db: AsyncSession) -> None:
    """A row excluded as not_a_startup with 'personal homepage' detail is re-queued."""
    co = _co(
        "Abnormal Security",
        "abnormal-security",
        exclusion_reason="not_a_startup",
        exclusion_detail=(
            "Resolved URL is a personal homepage, not a company site."
        ),
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.false_exclusion_requeued == 1

    await db.refresh(co)
    assert co.exclusion_reason is None
    assert co.exclusion_detail is None
    assert co.excluded_at is None
    assert co.eligibility_checked_at is None


async def test_wrong_site_exclusion_requeued(db: AsyncSession) -> None:
    """A row excluded as non_us with 'wrong site' detail is re-queued."""
    co = _co(
        "Soluto Corp",
        "soluto-corp",
        exclusion_reason="non_us",
        exclusion_detail="Resolved to wrong site — foreign company.",
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.false_exclusion_requeued == 1

    await db.refresh(co)
    assert co.exclusion_reason is None
    assert co.exclusion_detail is None


async def test_manual_exclusion_not_requeued(db: AsyncSession) -> None:
    """A row with reason='manual' is never touched by pass (c)."""
    co = _co(
        "Fal Legacy",
        "fal-legacy",
        exclusion_reason="manual",
        exclusion_detail="Franklin Associates — not the AI startup fal.ai.",
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.false_exclusion_requeued == 0
    await db.refresh(co)
    assert co.exclusion_reason == "manual"


async def test_legitimate_exclusion_not_requeued(db: AsyncSession) -> None:
    """A row excluded as not_a_startup without a wrong-site detail stays excluded."""
    co = _co(
        "Some VC Firm",
        "some-vc-firm",
        exclusion_reason="not_a_startup",
        exclusion_detail="This is a venture capital firm, not a startup.",
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.false_exclusion_requeued == 0
    await db.refresh(co)
    assert co.exclusion_reason == "not_a_startup"


# ── Idempotency ───────────────────────────────────────────────────────────────


async def test_idempotent_aggregator_url(db: AsyncSession) -> None:
    """Second run after pass (a) repair finds nothing to fix."""
    co = _co(
        "Idempotent Co A",
        "idempotent-co-a",
        website="https://crunchbase.com/organization/idempotent-co-a",
        description_short="Some company.",
    )
    db.add(co)
    await db.commit()

    first = await run_repair_wrong_websites(db)
    assert first.aggregator_url_reset == 1

    second = await run_repair_wrong_websites(db)
    assert second.aggregator_url_reset == 0
    assert second.parked_desc_reset == 0
    assert second.false_exclusion_requeued == 0


async def test_idempotent_parked_desc(db: AsyncSession) -> None:
    """Second run after pass (b) repair finds nothing to fix."""
    co = _co(
        "Idempotent Co B",
        "idempotent-co-b",
        website="https://idempotent.ai",
        description_short="This domain is for sale. Buy this domain now.",
    )
    db.add(co)
    await db.commit()

    first = await run_repair_wrong_websites(db)
    assert first.parked_desc_reset == 1

    second = await run_repair_wrong_websites(db)
    assert second.parked_desc_reset == 0
    assert second.aggregator_url_reset == 0
    assert second.false_exclusion_requeued == 0


async def test_idempotent_false_exclusion(db: AsyncSession) -> None:
    """Second run after pass (c) repair finds nothing to fix."""
    co = _co(
        "Idempotent Co C",
        "idempotent-co-c",
        exclusion_reason="not_a_startup",
        exclusion_detail="Resolved to a personal homepage of an unrelated person.",
    )
    db.add(co)
    await db.commit()

    first = await run_repair_wrong_websites(db)
    assert first.false_exclusion_requeued == 1

    second = await run_repair_wrong_websites(db)
    assert second.false_exclusion_requeued == 0
    assert second.aggregator_url_reset == 0
    assert second.parked_desc_reset == 0


# ── dry_run ───────────────────────────────────────────────────────────────────


async def test_dry_run_writes_nothing(db: AsyncSession) -> None:
    """--dry-run counts repairs without committing any changes."""
    co_a = _co(
        "Dry Run A",
        "dry-run-a",
        website="https://ycombinator.com/companies/dry-run-a",
        description_short="Some YC company.",
    )
    co_b = _co(
        "Dry Run B",
        "dry-run-b",
        website="https://dry-run-b.com",
        description_short="This domain is parked for sale.",
    )
    co_c = _co(
        "Dry Run C",
        "dry-run-c",
        exclusion_reason="non_us",
        exclusion_detail="Site resolved to a personal homepage.",
    )
    db.add_all([co_a, co_b, co_c])
    await db.commit()

    summary = await run_repair_wrong_websites(db, dry_run=True)
    assert summary.dry_run is True
    assert summary.aggregator_url_reset == 1
    assert summary.parked_desc_reset == 1
    assert summary.false_exclusion_requeued == 1

    # Nothing was actually written
    await db.refresh(co_a)
    assert co_a.website is not None
    await db.refresh(co_b)
    assert co_b.description_short is not None
    await db.refresh(co_c)
    assert co_c.exclusion_reason == "non_us"


# ── Pass (d): for-sale / parked PAGE content ─────────────────────────────────


async def test_for_sale_page_content_cleared(db: AsyncSession) -> None:
    """The real Foodology shape: the LLM narrated a for-sale lander as a real
    'culinary content platform', so the description escapes pass (b) — but the
    scraped page literally says '<host> is for sale'. Pass (d) re-judges the
    page content (ground truth) and resets the row."""
    lander = _co(
        "Foodology",
        "foodology-rww-d",
        website="https://foodology.com",
        description_short=(
            "Foodology is a culinary content platform exploring global "
            "traditions, based on a site that is currently for sale."
        ),
        description_long="A culinary content platform.",
        primary_category="content",
    )
    db.add(lander)
    await db.flush()
    db.add(
        RawPage(
            company_id=lander.id,
            url="https://foodology.com/",
            content=(
                "foodology.com is for sale.\n\nExploring Culinary Delights with "
                "Foodology\n\nDiscovering Global Culinary Traditions."
            ),
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)

    await db.refresh(lander)
    assert lander.website is None
    assert lander.description_short is None
    assert lander.description_long is None
    assert "https://foodology.com" in lander.rejected_urls
    assert (
        (await db.execute(select(RawPage).where(RawPage.company_id == lander.id)))
        .scalars()
        .all()
        == []
    )
    # description "...a site that is currently for sale" does NOT match pass (b)'s
    # regex (which needs "site is for sale"); pass (d) is what caught it.
    assert summary.parked_desc_reset == 0
    assert summary.page_content_reset == 1


async def test_for_sale_page_content_ignores_real_company(db: AsyncSession) -> None:
    """A real company whose homepage copy mentions selling is NOT reset by (d)."""
    real = _co(
        "SellRaze",
        "sellraze-rww-d",
        website="https://sellraze.com",
        description_short="SellRaze lists your items for sale across marketplaces.",
    )
    db.add(real)
    await db.flush()
    db.add(
        RawPage(
            company_id=real.id,
            url="https://sellraze.com/",
            content=(
                "SellRaze | The fastest way to sell your stuff\n"
                "List items for sale across every marketplace."
            ),
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.page_content_reset == 0
    await db.refresh(real)
    assert real.website == "https://sellraze.com"
    assert real.description_short is not None


async def test_idempotent_page_content(db: AsyncSession) -> None:
    """Second run after pass (d) repair finds nothing to fix."""
    lander = _co(
        "Citadel AI Lander",
        "citadel-ai-lander",
        website="https://citadel.ai",
        description_short="Citadel offers AI tooling for enterprises.",
    )
    db.add(lander)
    await db.flush()
    db.add(
        RawPage(
            company_id=lander.id,
            url="https://citadel.ai/",
            content=(
                "citadel.ai for sale | Spaceship.com\n"
                "citadel.ai is for sale on Spaceship. Secure checkout and transfer."
            ),
        )
    )
    await db.commit()

    first = await run_repair_wrong_websites(db)
    assert first.page_content_reset == 1
    second = await run_repair_wrong_websites(db)
    assert second.page_content_reset == 0
    assert second.aggregator_url_reset == 0
    assert second.parked_desc_reset == 0


async def test_for_sale_page_content_ignores_available_for_purchase(
    db: AsyncSession,
) -> None:
    """At-Bay regression: a real company whose page says 'available for purchase'
    (a resolver-detector phrase) must NOT be reset by pass (d).

    The page-content backfill scans a real company's full homepage text, so it
    uses the stricter page_is_for_sale_lander (self-referential domain-sale
    language only), not the resolver's looser text_looks_parked."""
    real = _co(
        "At-Bay",
        "at-bay-rww-d",
        website="https://www.at-bay.com/",
        description_short="At-Bay is a cyber insurance and security platform.",
    )
    db.add(real)
    await db.flush()
    db.add(
        RawPage(
            company_id=real.id,
            url="https://www.at-bay.com/",
            content=(
                "At-Bay: Cyber Insurance & MDR Security Platform | Proactive "
                "Protection\nAt-Bay's cyber insurance is available for purchase "
                "through licensed brokers."
            ),
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.page_content_reset == 0
    await db.refresh(real)
    assert real.website == "https://www.at-bay.com/"
    assert real.description_short is not None


# ── Pass (e): wrong-company profile ──────────────────────────────────────────


async def test_wrong_company_profile_reset_kalshi(db: AsyncSession) -> None:
    """The Kalshi/FrenFlow incident.

    Kalshi's stored profile is about FrenFlow: the resolver landed on FrenFlow's
    site (which merely lists Kalshi as a venue), so description_short opens
    'FrenFlow is ...' and the scraped page's title is 'FrenFlow — ...'. Pass (e)
    double-confirms the mismatch and resets the row."""
    kalshi = _co(
        "Kalshi",
        "kalshi-rww-e",
        website="https://frenflow.com",
        description_short=(
            "FrenFlow is a multi-venue prediction-market platform that lets you "
            "copy-trade the sharpest traders across Polymarket, Kalshi, "
            "Predict.fun, and Hyperliquid from one dashboard."
        ),
        description_long="A copy-trading platform for prediction markets.",
        primary_category="fintech",
    )
    db.add(kalshi)
    await db.flush()
    db.add(
        RawPage(
            company_id=kalshi.id,
            url="https://frenflow.com/",
            content=(
                "FrenFlow — Multi-Venue Prediction Market Platform\n"
                "Copy-trade prediction markets across Polymarket, Kalshi, "
                "Predict.fun and Hyperliquid."
            ),
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.wrong_company_reset == 1

    await db.refresh(kalshi)
    assert kalshi.website is None
    assert kalshi.description_short is None
    assert kalshi.description_long is None
    assert "https://frenflow.com" in kalshi.rejected_urls
    assert (
        (await db.execute(select(RawPage).where(RawPage.company_id == kalshi.id)))
        .scalars()
        .all()
        == []
    )


async def test_wrong_company_profile_reset_agentmail(db: AsyncSession) -> None:
    """AgentMail rendered a 'Series V' description — a different company."""
    agentmail = _co(
        "AgentMail",
        "agentmail-rww-e",
        website="https://seriesv.example",
        description_short=(
            "Series V provides early-stage venture capital to technical founders "
            "building developer infrastructure."
        ),
    )
    db.add(agentmail)
    await db.flush()
    db.add(
        RawPage(
            company_id=agentmail.id,
            url="https://seriesv.example/",
            content=(
                "Series V — Capital for technical founders\n"
                "We back developer-first companies from pre-seed to Series A."
            ),
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.wrong_company_reset == 1
    await db.refresh(agentmail)
    assert agentmail.website is None
    assert agentmail.description_short is None


async def test_wrong_company_correct_match_not_flagged(db: AsyncSession) -> None:
    """PRECISION GUARD: a correctly-matched company (description subject ==
    company name) is NEVER flagged by pass (e)."""
    ramp = _co(
        "Ramp",
        "ramp-rww-e",
        website="https://ramp.com",
        description_short=(
            "Ramp is an all-in-one spend management platform that combines "
            "corporate cards, bill pay, and accounting automation."
        ),
        description_long="Spend management for finance teams.",
    )
    db.add(ramp)
    await db.flush()
    db.add(
        RawPage(
            company_id=ramp.id,
            url="https://ramp.com/",
            content=(
                "Ramp — The all-in-one finance platform\n"
                "Ramp is the corporate card and spend management platform that "
                "helps finance teams save time and money."
            ),
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.wrong_company_reset == 0
    await db.refresh(ramp)
    assert ramp.website == "https://ramp.com"
    assert ramp.description_short is not None


async def test_wrong_company_suffix_variant_not_flagged(db: AsyncSession) -> None:
    """PRECISION GUARD: a description that opens with the company name plus a
    corporate suffix (or vice-versa) is NOT a mismatch."""
    co = _co(
        "Kalshi",
        "kalshi-suffix-rww-e",
        website="https://kalshi.com",
        description_short=(
            "Kalshi Inc operates the first CFTC-regulated prediction market in "
            "the United States."
        ),
    )
    db.add(co)
    await db.flush()
    db.add(
        RawPage(
            company_id=co.id,
            url="https://kalshi.com/",
            content=(
                "Kalshi — Trade on the outcome of events\n"
                "Kalshi is the first regulated prediction market in the US."
            ),
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.wrong_company_reset == 0
    await db.refresh(co)
    assert co.website == "https://kalshi.com"


async def test_wrong_company_unrecognized_opener_not_flagged(
    db: AsyncSession,
) -> None:
    """PRECISION GUARD: a description that does NOT open with a '<Name> <verb>'
    pattern yields no extractable subject, so no mismatch is asserted — even
    though the page title is a different brand."""
    co = _co(
        "Acme",
        "acme-noopener-rww-e",
        website="https://acme.com",
        description_short=(
            "An all-in-one platform for engineering teams to ship software faster."
        ),
    )
    db.add(co)
    await db.flush()
    db.add(
        RawPage(
            company_id=co.id,
            url="https://acme.com/",
            content="Some Other Brand — Homepage\nWelcome to our site.",
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.wrong_company_reset == 0
    await db.refresh(co)
    assert co.website == "https://acme.com"
    assert co.description_short is not None


async def test_wrong_company_corroboration_guard_page_is_company(
    db: AsyncSession,
) -> None:
    """PRECISION GUARD: even if the description opens with a different name, pass
    (e) does NOT fire when the stored page title IS dominated by the company —
    the page is genuinely the company's, so the odd description is left for
    re-enrichment, not treated as a wrong-site match."""
    co = _co(
        "Ramp",
        "ramp-corrob-rww-e",
        website="https://ramp.com",
        # Description opens naming a different company (a one-off LLM slip)...
        description_short="Brex is a corporate card company for startups.",
    )
    db.add(co)
    await db.flush()
    db.add(
        RawPage(
            company_id=co.id,
            url="https://ramp.com/",
            # ...but the page title is unambiguously Ramp's.
            content=(
                "Ramp — The all-in-one finance platform\n"
                "Ramp helps finance teams save time and money."
            ),
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.wrong_company_reset == 0
    await db.refresh(co)
    assert co.website == "https://ramp.com"


async def test_wrong_company_no_raw_page_not_flagged(db: AsyncSession) -> None:
    """PRECISION GUARD: with no scraped page to corroborate, pass (e) cannot
    confirm the mismatch and leaves the row alone."""
    co = _co(
        "Kalshi",
        "kalshi-nopage-rww-e",
        website="https://frenflow.com",
        description_short="FrenFlow is a multi-venue prediction-market platform.",
    )
    db.add(co)
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.wrong_company_reset == 0
    await db.refresh(co)
    assert co.website == "https://frenflow.com"


async def test_wrong_company_excluded_row_not_flagged(db: AsyncSession) -> None:
    """Excluded rows are out of scope for pass (e) (hidden already)."""
    co = _co(
        "Kalshi",
        "kalshi-excl-rww-e",
        website="https://frenflow.com",
        description_short="FrenFlow is a multi-venue prediction-market platform.",
        exclusion_reason="not_a_startup",
        exclusion_detail="Looks like a directory.",
    )
    db.add(co)
    await db.flush()
    db.add(
        RawPage(
            company_id=co.id,
            url="https://frenflow.com/",
            content="FrenFlow — Multi-Venue Prediction Market Platform\nCopy-trade.",
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.wrong_company_reset == 0
    await db.refresh(co)
    assert co.website == "https://frenflow.com"


async def test_idempotent_wrong_company(db: AsyncSession) -> None:
    """Second run after pass (e) repair finds nothing to fix."""
    co = _co(
        "Kalshi",
        "kalshi-idem-rww-e",
        website="https://frenflow.com",
        description_short=(
            "FrenFlow is a multi-venue prediction-market platform across "
            "Polymarket, Kalshi, and Predict.fun."
        ),
    )
    db.add(co)
    await db.flush()
    db.add(
        RawPage(
            company_id=co.id,
            url="https://frenflow.com/",
            content="FrenFlow — Multi-Venue Prediction Market Platform\nCopy-trade.",
        )
    )
    await db.commit()

    first = await run_repair_wrong_websites(db)
    assert first.wrong_company_reset == 1
    second = await run_repair_wrong_websites(db)
    assert second.wrong_company_reset == 0
    assert second.aggregator_url_reset == 0
    assert second.parked_desc_reset == 0
    assert second.page_content_reset == 0


async def test_wrong_company_dry_run_writes_nothing(db: AsyncSession) -> None:
    """--dry-run counts a pass (e) candidate without mutating it."""
    co = _co(
        "Kalshi",
        "kalshi-dry-rww-e",
        website="https://frenflow.com",
        description_short=(
            "FrenFlow is a multi-venue prediction-market platform across "
            "Polymarket, Kalshi, and Predict.fun."
        ),
    )
    db.add(co)
    await db.flush()
    db.add(
        RawPage(
            company_id=co.id,
            url="https://frenflow.com/",
            content="FrenFlow — Multi-Venue Prediction Market Platform\nCopy-trade.",
        )
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db, dry_run=True)
    assert summary.dry_run is True
    assert summary.wrong_company_reset == 1
    await db.refresh(co)
    assert co.website == "https://frenflow.com"
    assert co.description_short is not None
    assert (
        len(
            (await db.execute(select(RawPage).where(RawPage.company_id == co.id)))
            .scalars()
            .all()
        )
        == 1
    )


async def test_wrong_site_rounds_and_articles_deleted_on_reset(
    db: AsyncSession,
) -> None:
    """The helix/machinebrief incident (2026-07-16 QA): a news site accepted as
    the homepage gets its mined rounds + syndicated articles deleted on reset —
    same host only; a round citing a real third-party publisher survives."""
    co = _co(
        "Helix Digital",
        "helix-rww-rounds",
        website="https://machinebrief.example",
        description_short=(
            "Machine Brief is an AI news and analysis platform covering "
            "startups and enterprise adoption."
        ),
    )
    db.add(co)
    await db.flush()
    db.add(
        RawPage(
            company_id=co.id,
            url="https://machinebrief.example/",
            content=(
                "Machine Brief — AI news and analysis\n"
                "Daily coverage of AI startups and funding."
            ),
        )
    )
    # A round mined FROM the wrong site (same host) and a round citing a real
    # publisher (different host) — only the first must go.
    bad_round = FundingRound(
        company_id=co.id,
        round_type="Series A",
        amount_raised=28_000_000,
        primary_news_url="https://machinebrief.example/news/coval-raises-28m",
    )
    good_round = FundingRound(
        company_id=co.id,
        round_type=None,
        amount_raised=10_000_000_000,
        primary_news_url="https://siliconangle.example/helix-launches",
    )
    db.add_all([bad_round, good_round])
    await db.flush()
    good_round_id = good_round.id
    db.add_all(
        [
            NewsArticle(
                company_id=co.id,
                url="https://machinebrief.example/news/coval-raises-28m",
                title="Coval raises $28M",
                source="machinebrief.example",
                raw_content="body",
                processed=True,
            ),
            NewsArticle(
                company_id=co.id,
                url="https://siliconangle.example/helix-launches",
                title="Helix launches with $10B+",
                source="siliconangle.example",
                raw_content="body",
                processed=True,
            ),
        ]
    )
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.wrong_company_reset == 1
    assert summary.wrong_site_rounds_deleted == 1
    assert summary.wrong_site_articles_deleted == 1

    rounds = (
        (
            await db.execute(
                select(FundingRound).where(FundingRound.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert [r.id for r in rounds] == [good_round_id]  # third-party round kept
    articles = (
        (
            await db.execute(
                select(NewsArticle).where(NewsArticle.company_id == co.id)
            )
        )
        .scalars()
        .all()
    )
    assert [a.url for a in articles] == [
        "https://siliconangle.example/helix-launches"
    ]
    await db.refresh(co)
    assert co.website is None
    assert co.funding_round_count == 1  # denormalized count refreshed


async def test_aggregator_reset_never_deletes_news_sourced_rounds(
    db: AsyncSession,
) -> None:
    """The techcrunch hazard: a company whose website was wrongly set to a NEWS
    publisher (in AGGREGATOR_HOSTS) gets its website reset by pass (a), but its
    legitimately news-sourced rounds on that SAME host must survive — the purge
    requires wrong-company evidence, and this profile correctly names itself."""
    co = _co(
        "Acme Robotics",
        "acme-rww-tc",
        website="https://techcrunch.com/2026/01/acme-profile",
        description_short="Acme Robotics is a warehouse automation startup.",
    )
    db.add(co)
    await db.flush()
    legit_round = FundingRound(
        company_id=co.id,
        round_type="Series A",
        amount_raised=40_000_000,
        primary_news_url="https://techcrunch.com/2026/01/acme-raises-40m",
    )
    db.add(legit_round)
    await db.flush()
    round_id = legit_round.id
    await db.commit()

    summary = await run_repair_wrong_websites(db)
    assert summary.aggregator_url_reset == 1
    assert summary.wrong_site_rounds_deleted == 0  # legit rounds untouched

    await db.refresh(co)
    assert co.website is None  # the bad homepage IS reset
    survivor = (
        await db.execute(select(FundingRound).where(FundingRound.id == round_id))
    ).scalar_one_or_none()
    assert survivor is not None
