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

from nous.db.models import Company, RawPage
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
