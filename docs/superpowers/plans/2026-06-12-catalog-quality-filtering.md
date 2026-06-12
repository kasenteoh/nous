# Catalog Quality Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop junk entries (parse-artifact names, parked-domain websites, non-startups, husk rows) from rendering in the nous catalog, per the approved spec `docs/superpowers/specs/2026-06-12-catalog-quality-filtering-design.md`.

**Architecture:** Soft exclusion via a nullable `companies.exclusion_reason` column set by the pipeline; structured `website_state`/`is_startup` signals from the enrichment LLM instead of prose; a one-time idempotent `repair-catalog` stage for the 96 Lightspeed name artifacts and ~30 parked-domain rows; a uniform "catalog bar" filter (`exclusion_reason IS NULL AND (description_short IS NOT NULL OR funding_round_count > 0)`) applied to every catalog-facing web query.

**Tech Stack:** Python 3.11 / SQLAlchemy 2 async / Alembic / Pydantic v2 / selectolax / pytest (DB tests gated on `DATABASE_URL`); Next.js 16 + supabase-js (PostgREST) on the web side.

**Branch:** `catalog-quality-filtering` (already exists; spec is committed there).

**Verified facts this plan relies on** (do not re-derive):
- lsvp.com portfolio cards: `<li data-company-id="..." data-investor="lsvp|lsip|both">`; the `h5` holds the name as a *direct* text node plus a nested `span.info-icon-wrapper` whose disclaimer text ("LSVP and LSIP Investment" / "LSIP Investment") bleeds into `h5.text()`. `h5.text(deep=False, strip=True)` returns just the name (verified against live HTML). Live counts: lsvp=557, lsip=65, both=31 — and 65+31=96 equals the suffixed rows in prod.
- The existing fixture `pipeline/tests/fixtures/vc_portfolios/lightspeed.html` is a full capture **with** the badges (31 `both`, 65 `lsip`, 555 `lsvp`) — reuse it, do not recapture.
- All LLM calls run on DeepSeek (paid, cheap) via `nous.llm.client.complete_json`; rate-limit handling pattern is `except LLMRateLimitError: break`.
- Prod data (2026-06-12): 4,218 companies; 96 suffixed names; ~30–41 parked-prose descriptions; 2,590 description-less.
- Web error paths all degrade to empty results, and pre-migration PostgREST 400s on unknown columns land on those paths (documented precedent: `getCompanyOgData` + migration 0021).

**LLM cost note (flag in the PR):** the `judge-eligibility` backfill is ~1,600 one-time DeepSeek calls ≈ $1–3 at current pricing, drained at `--limit 200/day` by the daily descriptions workflow. Steady state ≈ $0 (new enrichments stamp themselves).

---

## File map

| File | Action | Purpose |
|---|---|---|
| `pipeline/src/nous/db/models.py` | modify | 6 new `Company` columns + CHECK |
| `pipeline/alembic/versions/0022_catalog_quality_filtering.py` | create | hand-written migration + count backfill |
| `pipeline/src/nous/db/upsert.py` | modify | `refresh_funding_round_count`; call from `reconcile_funding_round` + `merge_companies` |
| `pipeline/src/nous/sources/vc_portfolios/lightspeed.py` | modify | skip `lsip` cards; `text(deep=False)` |
| `pipeline/src/nous/sources/parked.py` | create | `looks_parked(html)` detector |
| `pipeline/src/nous/sources/homepage.py` | modify | reject parked pages + `rejected_urls` domains in `resolve_homepage` |
| `pipeline/src/nous/llm/prompts/company_description.py` | modify | `website_state`, `is_startup`, `not_startup_reason`, `founded_year`, `hq_country` |
| `pipeline/src/nous/llm/prompts/company_eligibility.py` | create | backfill judgment prompt |
| `pipeline/src/nous/pipeline/enrich_companies.py` | modify | structured-signal reactions + exclusion skip |
| `pipeline/src/nous/pipeline/judge_eligibility.py` | create | backfill stage |
| `pipeline/src/nous/pipeline/repair_catalog.py` | create | one-time repair stage |
| `pipeline/src/nous/pipeline/exclude_company.py` | create | manual exclude/clear helper |
| `pipeline/src/nous/pipeline/{resolve_homepages,scrape_homepages,ingest_news,extract_funding,estimate_employees,analyze_competitors}.py` | modify | add `exclusion_reason IS NULL` to selections |
| `pipeline/src/nous/cli.py` | modify | `repair-catalog`, `judge-eligibility`, `exclude-company` commands |
| `pipeline/tests/test_vc_portfolios.py` | modify | badge regression tests |
| `pipeline/tests/{test_parked.py,test_repair_catalog.py,test_judge_eligibility.py,test_quality_columns.py}` | create | new tests |
| `pipeline/tests/{test_enrich_companies.py,test_upsert.py,test_resolve_homepages.py}` | modify | new behavior tests + constructor updates |
| `web/lib/types.ts` | modify | optional `exclusion_reason` / `funding_round_count` on `CompanyRow` |
| `web/lib/queries.ts` | modify | catalog bar everywhere |
| `web/lib/spotlight.ts` | modify | exclusion filter on company selects |
| `.github/workflows/descriptions.yml` | modify | `repair-catalog` + `judge-eligibility` steps |
| `docs/superpowers/specs/2026-06-12-catalog-quality-filtering-design.md` | modify | fix stale "Gemini free-tier" wording |

Environment for every pipeline step: run from `pipeline/`, with the local test DB env var exported once per shell:

```bash
cd pipeline
export DATABASE_URL=$(grep -h '^DATABASE_URL' .env | cut -d= -f2- | sed 's/^"//;s/"$//')
```

(Do NOT `source .env` — see repo memory. Tests skip silently when `DATABASE_URL` is unset, so verify it is set before trusting a green run.)

---

### Task 1: Schema — quality columns + migration 0022

**Files:**
- Modify: `pipeline/src/nous/db/models.py` (Company class, after the `total_raised_*` block ~line 165)
- Create: `pipeline/alembic/versions/0022_catalog_quality_filtering.py`
- Create: `pipeline/tests/test_quality_columns.py`

- [ ] **Step 1: Write the failing round-trip test**

```python
"""Round-trip test for the catalog-quality columns added in migration 0022."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


@pytest.mark.asyncio
async def test_quality_columns_round_trip(db: AsyncSession) -> None:
    company = Company(
        name="Junk Co",
        slug="junk-co-quality-test",
        normalized_name="junk co quality test",
        hq_country="US",
        exclusion_reason="not_a_startup",
        exclusion_detail="founded 1999; public company",
        excluded_at=datetime.now(tz=UTC),
        eligibility_checked_at=datetime.now(tz=UTC),
        rejected_urls=["https://junk.ai"],
        funding_round_count=2,
    )
    db.add(company)
    await db.commit()

    row = (
        await db.execute(select(Company).where(Company.slug == "junk-co-quality-test"))
    ).scalar_one()
    assert row.exclusion_reason == "not_a_startup"
    assert row.rejected_urls == ["https://junk.ai"]
    assert row.funding_round_count == 2


@pytest.mark.asyncio
async def test_quality_columns_defaults(db: AsyncSession) -> None:
    company = Company(
        name="Default Co",
        slug="default-co-quality-test",
        normalized_name="default co quality test",
        hq_country="US",
    )
    db.add(company)
    await db.commit()
    await db.refresh(company)

    assert company.exclusion_reason is None
    assert company.rejected_urls == []
    assert company.funding_round_count == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_quality_columns.py -v`
Expected: FAIL / ERROR — `TypeError: 'exclusion_reason' is an invalid keyword argument for Company()` (model lacks the columns).

- [ ] **Step 3: Add the columns to the Company model**

In `pipeline/src/nous/db/models.py`, extend the imports (`text` from sqlalchemy):

```python
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
```

Add a second CheckConstraint to `Company.__table_args__`:

```python
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'acquired', 'shut_down', 'ipo')",
            name="ck_companies_status",
        ),
        CheckConstraint(
            "exclusion_reason IN ('parse_artifact', 'non_us', 'not_a_startup', 'manual') "
            "OR exclusion_reason IS NULL",
            name="ck_companies_exclusion_reason",
        ),
    )
```

Append to the Company class body (after the `total_raised_*` block):

```python
    # Catalog-quality soft exclusion (spec 2026-06-12). NULL = included.
    # 'parse_artifact' | 'non_us' | 'not_a_startup' | 'manual' (CHECK above).
    # Set by enrich-companies / judge-eligibility / repair-catalog / the
    # exclude-company CLI; NEVER cleared by discovery (re-appearing on a VC
    # portfolio page is not new evidence). Indexed: every catalog query and
    # every pipeline selection filters on IS NULL.
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    exclusion_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    excluded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # When the is-this-a-startup judgment last ran (enrich path or the
    # judge-eligibility backfill). Lets the backfill find enriched-but-unjudged
    # rows exactly once. Indexed for that WHERE.
    eligibility_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # URLs confirmed NOT to be this company's site (parked/for-sale or an
    # unrelated business) — resolve-homepages must never re-pick a domain in
    # here. JSONB list of strings; ALWAYS reassign (rejected_urls = [*old, new]),
    # never append in place — plain JSONB columns don't track mutation.
    rejected_urls: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # Denormalized count(funding_rounds) maintained by reconcile_funding_round
    # + merge_companies and backfilled in migration 0022. Exists so the web
    # catalog bar (description OR funded) is a flat indexed WHERE — PostgREST
    # can't paginate an OR over an EXISTS subquery.
    funding_round_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", index=True
    )
```

- [ ] **Step 4: Write migration 0022 (hand-written, like 0021 — autogenerate emits spurious DROPs)**

Create `pipeline/alembic/versions/0022_catalog_quality_filtering.py`:

```python
"""Catalog quality filtering: soft exclusion + content-bar columns.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-12 00:00:00.000000

Per docs/superpowers/specs/2026-06-12-catalog-quality-filtering-design.md:

- ``exclusion_reason`` / ``exclusion_detail`` / ``excluded_at`` — soft
  exclusion with an audit trail. NULL reason = included. Soft (not DELETE)
  because weekly portfolio re-discovery would resurrect deleted rows.
- ``eligibility_checked_at`` — stamp for the is-this-a-startup judgment so
  the judge-eligibility backfill visits each enriched row exactly once.
- ``rejected_urls`` — JSONB list of URLs confirmed wrong for the company
  (parked/for-sale domains); resolve-homepages skips these domains.
- ``funding_round_count`` — denormalized count(funding_rounds), backfilled
  below, so the web catalog bar is a flat indexed WHERE instead of an OR
  over an EXISTS that PostgREST cannot express with pagination.

Hand-written rather than autogenerated: autogenerate emits spurious DROPs
for hand-created objects (trigram GIN, partial indexes) it cannot model.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies", sa.Column("exclusion_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "companies", sa.Column("exclusion_detail", sa.Text(), nullable=True)
    )
    op.add_column(
        "companies",
        sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column(
            "eligibility_checked_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "rejected_urls",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "companies",
        sa.Column(
            "funding_round_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_companies_exclusion_reason",
        "companies",
        "exclusion_reason IN ('parse_artifact', 'non_us', 'not_a_startup', 'manual') "
        "OR exclusion_reason IS NULL",
    )
    op.create_index(
        "ix_companies_exclusion_reason", "companies", ["exclusion_reason"]
    )
    op.create_index(
        "ix_companies_eligibility_checked_at",
        "companies",
        ["eligibility_checked_at"],
    )
    op.create_index(
        "ix_companies_funding_round_count", "companies", ["funding_round_count"]
    )

    # Backfill the denormalized round count from existing data.
    op.execute(
        """
        UPDATE companies
        SET funding_round_count = sub.cnt
        FROM (
            SELECT company_id, count(*) AS cnt
            FROM funding_rounds
            GROUP BY company_id
        ) AS sub
        WHERE companies.id = sub.company_id
        """
    )


def downgrade() -> None:
    op.drop_index("ix_companies_funding_round_count", table_name="companies")
    op.drop_index("ix_companies_eligibility_checked_at", table_name="companies")
    op.drop_index("ix_companies_exclusion_reason", table_name="companies")
    op.drop_constraint(
        "ck_companies_exclusion_reason", "companies", type_="check"
    )
    op.drop_column("companies", "funding_round_count")
    op.drop_column("companies", "rejected_urls")
    op.drop_column("companies", "eligibility_checked_at")
    op.drop_column("companies", "excluded_at")
    op.drop_column("companies", "exclusion_detail")
    op.drop_column("companies", "exclusion_reason")
```

- [ ] **Step 5: Apply the migration and run the test**

Run: `uv run alembic upgrade head`
Expected: `Running upgrade 0021 -> 0022, Catalog quality filtering...`

Run: `uv run pytest tests/test_quality_columns.py -v`
Expected: 2 PASSED.

- [ ] **Step 6: Run lint/type/full suite**

Run: `uv run ruff check . && uv run mypy src && uv run pytest -q`
Expected: all pass (existing tests unaffected — new columns have server defaults).

- [ ] **Step 7: Commit**

```bash
git add src/nous/db/models.py alembic/versions/0022_catalog_quality_filtering.py tests/test_quality_columns.py
git commit -m "feat(db): catalog-quality columns — soft exclusion, rejected_urls, funding_round_count"
```

---

### Task 2: `funding_round_count` maintenance

**Files:**
- Modify: `pipeline/src/nous/db/upsert.py` (`reconcile_funding_round` ~line 397-409; `merge_companies` after the funding_rounds repoint ~line 675)
- Modify: `pipeline/tests/test_upsert.py` (append tests)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_upsert.py`, reusing its existing imports/helpers — it already imports `Company`, the `db` fixture pattern, and `reconcile_funding_round`/`merge_companies` or add those imports if missing):

```python
from datetime import date

from nous.db.models import FundingRound
from nous.db.upsert import merge_companies, reconcile_funding_round
from nous.llm.prompts.funding_extraction import FundingExtraction


def _make_quality_company(name: str, slug: str) -> Company:
    return Company(
        name=name, slug=slug, normalized_name=slug.replace("-", " "), hq_country="US"
    )


@pytest.mark.asyncio
async def test_reconcile_funding_round_maintains_count(db: AsyncSession) -> None:
    company = _make_quality_company("Counted Co", "counted-co")
    db.add(company)
    await db.flush()

    extraction = FundingExtraction(
        is_funding_announcement=True,
        round_type="Seed",
        amount_raised_usd=1_000_000,
        announced_date=date(2026, 5, 1),
        confidence="high",
    )
    _, created = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=extraction,
        primary_news_url="https://example.com/a",
    )
    assert created is True
    await db.refresh(company)
    assert company.funding_round_count == 1

    # Re-running the same extraction merges (created=False) and count stays 1.
    _, created = await reconcile_funding_round(
        db,
        company_id=company.id,
        extraction=extraction,
        primary_news_url="https://example.com/b",
    )
    assert created is False
    await db.refresh(company)
    assert company.funding_round_count == 1


@pytest.mark.asyncio
async def test_merge_companies_refreshes_survivor_count(db: AsyncSession) -> None:
    survivor = _make_quality_company("Survivor Co", "survivor-co")
    loser = _make_quality_company("Loser Co", "loser-co")
    db.add_all([survivor, loser])
    await db.flush()
    db.add(
        FundingRound(
            company_id=loser.id,
            round_type="Seed",
            announced_date=date(2026, 1, 1),
        )
    )
    await db.flush()

    await merge_companies(db, survivor_id=survivor.id, loser_id=loser.id)
    await db.refresh(survivor)
    assert survivor.funding_round_count == 1
```

(Constructor verified against `pipeline/src/nous/llm/prompts/funding_extraction.py`: `is_funding_announcement` is the only required field; `round_type`/`amount_raised_usd`/`announced_date`/`confidence` are optional and named exactly as used above.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_upsert.py -k "count" -v`
Expected: FAIL — `assert company.funding_round_count == 1` (stays 0).

- [ ] **Step 3: Implement the helper + call sites** in `pipeline/src/nous/db/upsert.py`:

```python
async def refresh_funding_round_count(
    session: AsyncSession, company_id: UUID
) -> None:
    """Recompute companies.funding_round_count from funding_rounds.

    Set-based and idempotent — safe to call after any round insert or merge.
    The denormalized count exists for the web catalog bar (see migration 0022).
    """
    cnt = (
        select(func.count())
        .select_from(FundingRound)
        .where(FundingRound.company_id == company_id)
        .scalar_subquery()
    )
    await session.execute(
        update(Company)
        .where(Company.id == company_id)
        .values(funding_round_count=cnt)
    )
```

In `reconcile_funding_round`, after the insert branch's `await session.flush()` (line ~408), add:

```python
    await session.flush()
    await refresh_funding_round_count(session, company_id)
    return new_round, True
```

In `merge_companies`, immediately after the funding_rounds repoint block (the `update(FundingRound)...values(company_id=survivor_id)` execute), add:

```python
    # Keep the denormalized catalog-bar count truthful for the survivor.
    await refresh_funding_round_count(session, survivor_id)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_upsert.py tests/test_extract_funding.py tests/test_dedup_companies.py -q`
Expected: PASS (existing extract/dedup suites exercise both call sites).

- [ ] **Step 5: Commit**

```bash
git add src/nous/db/upsert.py tests/test_upsert.py
git commit -m "feat(db): maintain denormalized funding_round_count on reconcile + merge"
```

---

### Task 3: Lightspeed adapter fix

**Files:**
- Modify: `pipeline/src/nous/sources/vc_portfolios/lightspeed.py`
- Modify: `pipeline/tests/test_vc_portfolios.py`

- [ ] **Step 1: Write the failing regression test** (append to `tests/test_vc_portfolios.py`, after `test_lightspeed_yields_no_websites` ~line 280; it uses the module's existing `_html_routes`/`_MockTransport`/`_inject_transport`/`FIXTURES`/`USER_AGENT` helpers):

```python
@pytest.mark.asyncio
async def test_lightspeed_strips_fund_badges_and_skips_india() -> None:
    """The h5 on lsvp.com nests a fund-badge span ("LSVP and LSIP Investment" /
    "LSIP Investment") that .text() used to concatenate into the company name
    (prod had 96 such rows). data-investor='lsip' marks Lightspeed India
    Partners-only holdings — out of scope for a US-only catalog."""
    adapter = ADAPTERS["lightspeed"]
    routes = _html_routes(adapter.PORTFOLIO_URL, FIXTURES / "lightspeed.html")  # type: ignore[attr-defined]
    transport = _MockTransport(routes)

    async with HomepageClient(USER_AGENT) as client:
        _inject_transport(client, transport)
        entries = await adapter.fetch(client)

    names = [e.name for e in entries]
    # No badge text may bleed into any name.
    assert not [n for n in names if "LSVP" in n or "LSIP" in n]
    # data-investor="both" cards are kept, with clean names (fixture has 31).
    assert "1047 games" in names
    # data-investor="lsip" cards (India-only, fixture has 65) are skipped.
    assert "Airbound" not in names
    # 557 lsvp + 31 both = ~588; assert a safe floor well above the lsvp-only count.
    assert len(names) >= 560
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_vc_portfolios.py -k lightspeed -v`
Expected: the new test FAILS on the badge assertion (names like `1047 gamesLSVP and LSIP Investment` present, `Airbound` present); the two pre-existing lightspeed tests still pass.

- [ ] **Step 3: Fix the adapter** — replace the `for item in ...` loop body in `pipeline/src/nous/sources/vc_portfolios/lightspeed.py` (lines 27-43) with:

```python
        for item in tree.css("ul.companies-list li[data-company-id]"):
            # data-investor marks which fund(s) hold the company:
            # 'lsvp' (US), 'lsip' (Lightspeed India Partners), 'both'.
            # India-only holdings are out of scope (US-only catalog).
            if item.attributes.get("data-investor") == "lsip":
                continue
            heading = item.css_first(".detail h5")
            if heading is None:
                continue
            # deep=False: the h5 nests a span.info-icon-wrapper fund-badge
            # ("LSVP and LSIP Investment") that deep text concatenates into
            # the name — the source of 96 mangled prod rows.
            name = heading.text(deep=False, strip=True)
            if not name or name in seen:
                continue
            seen.add(name)
            entries.append(
                PortfolioEntry(
                    firm=self.firm,
                    name=name,
                    website=None,
                    description=None,
                    source_url=self.PORTFOLIO_URL,
                )
            )
```

Also update the module docstring's last sentence to mention the badge/skip behavior.

- [ ] **Step 4: Run the adapter tests**

Run: `uv run pytest tests/test_vc_portfolios.py -q`
Expected: ALL PASS (including the parametrized `lightspeed.html` case expecting "Anthropic", ≥50).

- [ ] **Step 5: Commit**

```bash
git add src/nous/sources/vc_portfolios/lightspeed.py tests/test_vc_portfolios.py
git commit -m "fix(lightspeed): stop fund-badge text bleeding into names; skip India-only holdings"
```

---

### Task 4: Parked-page detector + resolver integration

**Files:**
- Create: `pipeline/src/nous/sources/parked.py`
- Create: `pipeline/tests/test_parked.py`
- Modify: `pipeline/src/nous/sources/homepage.py` (`resolve_homepage`, lines 300-367)
- Modify: `pipeline/src/nous/pipeline/resolve_homepages.py` (pass `rejected_urls`)
- Modify: `pipeline/tests/test_resolve_homepages.py` (selection/skip tests)

- [ ] **Step 1: Write the failing detector tests** — create `tests/test_parked.py`:

```python
"""Unit tests for the parked/for-sale page detector.

False positives are expensive (a real company website rejected); false
negatives are cheap (enrichment's website_state catches them later). The
detector is therefore deliberately conservative.
"""

from __future__ import annotations

from nous.sources.parked import looks_parked

SPACESHIP_PARKED = """
<html><head><title>9gag.ai is for sale</title></head><body>
<h1>9gag.ai</h1><p>This domain is for sale. Get it before someone else does.</p>
<a href="#">Buy now on Spaceship</a></body></html>
"""

GODADDY_PARKED = """
<html><head><title>cameo.ai</title></head><body>
<p>cameo.ai is parked free, courtesy of GoDaddy.com.</p>
<p>Would you like to buy this domain?</p></body></html>
"""

MARKETPLACE_PARKED = """
<html><head><title>Premium domain</title></head><body>
<p>The domain name enter.ai is for sale! Make an offer via Atom.com,
the leading domain marketplace.</p></body></html>
"""

REAL_HOMEPAGE = """
<html><head><title>Acme — ship faster</title></head><body>
<nav>Product Pricing About</nav>
<h1>Acme helps engineering teams ship faster</h1>
<p>Trusted by 400 companies. Read our customer stories.</p></body></html>
"""

# A real product whose copy mentions listing items for sale (the SellRaze
# case that a naive "for sale" pattern false-matched in prod analysis).
ECOMMERCE_HOMEPAGE = """
<html><head><title>SellRaze</title></head><body>
<h1>List items for sale across every marketplace</h1>
<p>SellRaze uses image recognition to identify, price, and list your items
for sale on eBay, Amazon, and more.</p></body></html>
"""


def test_detects_spaceship_style_sale_page() -> None:
    assert looks_parked(SPACESHIP_PARKED) is True


def test_detects_godaddy_parking() -> None:
    assert looks_parked(GODADDY_PARKED) is True


def test_detects_marketplace_listing() -> None:
    assert looks_parked(MARKETPLACE_PARKED) is True


def test_real_homepage_not_parked() -> None:
    assert looks_parked(REAL_HOMEPAGE) is False


def test_ecommerce_copy_mentioning_for_sale_not_parked() -> None:
    assert looks_parked(ECOMMERCE_HOMEPAGE) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_parked.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nous.sources.parked'`.

- [ ] **Step 3: Implement the detector** — create `pipeline/src/nous/sources/parked.py`:

```python
"""Heuristic detector for parked / for-sale / registrar-placeholder pages.

Why: resolve_homepage accepts any 200 page whose text mentions the company
name — and a parked page ALWAYS mentions the domain name, which is how prod
attached parked 9gag.ai/substack.ai/cameo.ai to real companies. This check
runs before the name-mention acceptance.

Deliberately conservative: a false positive rejects a real company homepage
(expensive — the company stays website-less), while a false negative just
defers to enrichment's website_state signal (cheap). Standalone phrases must
be domain-sale specific; marketplace brand names alone only count when a
sale-intent phrase co-occurs ("powered by GoDaddy" on a real site must not
trip it, and product copy like "list items for sale" has no domain wording).
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

# Phrases that on their own mark a domain-sale/parking page (lowercase).
_SALE_PHRASES: tuple[str, ...] = (
    "this domain is for sale",
    "domain is for sale",
    "domain name is for sale",
    "domain for sale",
    "domain may be for sale",
    "buy this domain",
    "purchase this domain",
    "is parked free",
    "parked domain",
    "domain parking",
    "domain marketplace",
)

# Registrar / domain-marketplace brands: only parked when a sale-intent
# phrase co-occurs (brand names appear in footers of real sites).
_MARKETPLACE_BRANDS: tuple[str, ...] = (
    "spaceship",
    "godaddy",
    "sedo",
    "afternic",
    "dan.com",
    "hugedomains",
    "atom.com",
    "saw.com",
    "squadhelp",
    "namecheap",
    "porkbun",
    "reg.ai",
)

_SALE_INTENT: tuple[str, ...] = ("for sale", "buy now", "make an offer", "make offer")


def looks_parked(html: str) -> bool:
    """True when *html* looks like a parked / for-sale / placeholder page."""
    tree = HTMLParser(html)
    text = tree.text(strip=True).lower()
    title_node = tree.css_first("title")
    if title_node is not None:
        text = f"{title_node.text(strip=True).lower()} {text}"

    if any(phrase in text for phrase in _SALE_PHRASES):
        return True
    if any(brand in text for brand in _MARKETPLACE_BRANDS) and any(
        intent in text for intent in _SALE_INTENT
    ):
        return True
    return False
```

- [ ] **Step 4: Run detector tests**

Run: `uv run pytest tests/test_parked.py -v`
Expected: 5 PASSED.

- [ ] **Step 5: Wire into `resolve_homepage`** in `pipeline/src/nous/sources/homepage.py`.

Add imports near the top (with the other `nous.` imports):

```python
from nous.sources.parked import looks_parked
from nous.util.url import canonical_domain
```

Change the signature and docstring (line 300):

```python
async def resolve_homepage(
    client: HomepageClient,
    slug_base: str,
    company_name: str,
    *,
    tlds: Iterable[str] = CANDIDATE_TLDS,
    rejected_urls: Iterable[str] = (),
) -> str | None:
    """Phase 1: try ``{slug_base}{tld}`` for each TLD in order.

    On a 200 response, rejects parked/for-sale pages (see nous.sources.parked)
    and any candidate whose canonical domain matches a previously rejected URL
    (``rejected_urls`` — confirmed-wrong domains recorded by enrichment), then
    validates that the page's visible text contains ``slug_base``
    (case-insensitive). Returns the first plausible URL on match.

    Phase 2: if all TLD guesses miss, query DuckDuckGo for
    ``"{company_name}" startup``, filter out aggregator domains, and return the
    first candidate whose page contains the company name (same parked/rejected
    checks).

    Returns None if both phases miss.
    """
    rejected_domains = {
        d for d in (canonical_domain(u) for u in rejected_urls) if d is not None
    }
```

In Phase 1, before the fetch, skip rejected domains; after the fetch, reject parked pages:

```python
    # Phase 1: TLD heuristic
    for tld in tlds:
        url = f"https://{slug_base}{tld}"
        if canonical_domain(url) in rejected_domains:
            continue
        try:
            result = await client.fetch(url)
        except RobotsBlockedError:
            continue
        except httpx.HTTPStatusError:
            continue
        except httpx.RequestError:
            continue

        # A parked page always mentions the domain name, so this check MUST
        # run before the name-mention acceptance below.
        if looks_parked(result.content):
            continue

        # Validate: does visible page text mention the slug?
        visible_text = HTMLParser(result.content).text(strip=True).lower()
        if slug_base.replace("-", " ") in visible_text or slug_base in visible_text:
            return result.url  # final URL after any redirects
```

In Phase 2's candidate loop, add the same two checks:

```python
    for candidate_url in candidates:
        if is_aggregator(candidate_url):
            continue
        if canonical_domain(candidate_url) in rejected_domains:
            continue
        try:
            result = await client.fetch(candidate_url)
        except (RobotsBlockedError, httpx.HTTPStatusError, httpx.RequestError):
            continue
        if looks_parked(result.content):
            continue
        visible_text = HTMLParser(result.content).text(strip=True).lower()
```

- [ ] **Step 6: Pass `rejected_urls` from the stage** — in `pipeline/src/nous/pipeline/resolve_homepages.py` line ~111:

```python
            resolved = await resolve_homepage(
                client,
                slug_base=slug_base,
                company_name=company.name,
                rejected_urls=company.rejected_urls or (),
            )
```

- [ ] **Step 7: Add an integration test** — append to `tests/test_resolve_homepages.py`. The file already defines `_make_company` and `MockHomepageClient` (a `HomepageClient` subclass whose `fetch` returns canned `FetchResult`s; its inherited `search_companies` raises outside a context manager, which `resolve_homepage` treats as "no DDG candidates" — the existing tests rely on the same behavior). Tests in this file are bare `async def` (asyncio auto mode):

```python
class _ParkedAwareClient(MockHomepageClient):
    """Serves a parked page for chosen hosts; records every fetched URL."""

    def __init__(self, parked_hosts: set[str], fetched: list[str]) -> None:
        super().__init__({})
        self._parked_hosts = parked_hosts
        self._fetched = fetched

    async def fetch(self, url: str) -> FetchResult:
        self._fetched.append(url)
        host = url.removeprefix("https://").removeprefix("http://").split("/")[0]
        if host in self._parked_hosts:
            return FetchResult(
                url=url,
                status_code=200,
                content=(
                    "<html><body>ninegag.com — this domain is for sale."
                    " Buy this domain on Spaceship.</body></html>"
                ),
                content_type="text/html",
            )
        raise httpx.RequestError(f"no route for {url}", request=None)  # type: ignore[arg-type]


async def test_resolver_rejects_parked_page_and_rejected_domains(
    db: AsyncSession,
) -> None:
    """Parked candidates are rejected even though they mention the name, and
    domains in rejected_urls are never fetched at all."""
    company = _make_company(name="Ninegag", slug="ninegag-parked-resolve")
    company.rejected_urls = ["https://ninegag.ai"]
    db.add(company)
    await db.flush()
    await db.commit()

    fetched: list[str] = []
    client = _ParkedAwareClient({"ninegag.com"}, fetched)
    summary = await run_resolve_homepages(db, client)

    await db.refresh(company)
    assert company.website is None  # parked page mentioning the name ≠ a match
    assert company.website_resolved_at is not None  # attempt is still stamped
    assert summary.no_match == 1
    # The parked .com was fetched and rejected by content...
    assert any(u.startswith("https://ninegag.com") for u in fetched)
    # ...the rejected .ai domain was skipped before any fetch.
    assert not any("ninegag.ai" in u for u in fetched)
```

- [ ] **Step 8: Run the resolver suites**

Run: `uv run pytest tests/test_parked.py tests/test_resolve_homepages.py tests/test_homepage.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/nous/sources/parked.py tests/test_parked.py src/nous/sources/homepage.py src/nous/pipeline/resolve_homepages.py tests/test_resolve_homepages.py
git commit -m "feat(resolve): reject parked/for-sale pages and previously rejected domains"
```

---

### Task 5: Enrichment structured signals

**Files:**
- Modify: `pipeline/src/nous/llm/prompts/company_description.py`
- Modify: `pipeline/src/nous/pipeline/enrich_companies.py`
- Modify: `pipeline/tests/test_enrich_companies.py`

- [ ] **Step 1: Extend the Pydantic model** — in `company_description.py` add after the `industry` field of `CompanyDescription`:

```python
    website_state: Literal[
        "ok",
        "parked_or_for_sale",
        "under_construction",
        "unrelated_site",
        "insufficient_info",
    ] = Field(
        ...,
        description=(
            "'ok' when the text reads like the company's own operating site. "
            "'parked_or_for_sale' for domain-sale/parking/registrar pages. "
            "'under_construction' for launching-soon/placeholder pages. "
            "'unrelated_site' when the text is about a DIFFERENT business "
            "than the named company. 'insufficient_info' when there is too "
            "little text to tell."
        ),
    )
    is_startup: bool | None = Field(
        default=None,
        description=(
            "True when this reads like an operating startup: an independent, "
            "private company founded within roughly the last 15 years. False "
            "when it clearly is not (decades-old enterprise, publicly traded, "
            "a subsidiary, a fund, a media site). Null when the text does not "
            "support a confident call — never guess."
        ),
    )
    not_startup_reason: str | None = Field(
        default=None,
        description="One short sentence; only when is_startup is false.",
    )
    founded_year: int | None = Field(
        default=None,
        description="Founding year ONLY if the text states it. Null otherwise.",
    )
    hq_country: str | None = Field(
        default=None,
        description=(
            "Headquarters country as a 2-letter ISO code (e.g. 'US', 'IN', "
            "'GB') ONLY when the text clearly states it. Null otherwise — "
            "never guess."
        ),
    )
```

Add `from typing import Literal` to the imports.

- [ ] **Step 2: Extend the prompt rules** — append to `PROMPT_TEMPLATE`'s `Rules:` list (before the `Company name:` line):

```
- `website_state`: classify the page itself. Use 'parked_or_for_sale' for
  domain-sale/parking/registrar placeholder pages, 'under_construction' for
  launching-soon pages with no product info, 'unrelated_site' when the text
  describes a different business than {company_name}, 'insufficient_info'
  when there is too little text to tell, and 'ok' otherwise. When the state
  is not 'ok', still fill the description fields with a one-line factual note
  (they will not be published).
- `is_startup`: true only for an independent, PRIVATE company founded within
  roughly the last 15 years. False for decades-old enterprises, publicly
  traded companies, subsidiaries, funds, or media properties. If the text
  does not support a confident call, return null. Never guess.
- `not_startup_reason`: one short factual sentence, only when is_startup is
  false (e.g. "Founded in 2000; publicly traded enterprise").
- `founded_year` / `hq_country`: only when the text states them. `hq_country`
  is a 2-letter ISO code. Null otherwise — never fabricate.
```

- [ ] **Step 3: Update the canned test fixtures** so the suite still constructs valid models. In `tests/test_enrich_companies.py` there are exactly three `CompanyDescription(` constructor sites (lines ~41, ~124, ~166): add `website_state="ok",` to each. Verify none remain:

Run: `grep -rn "CompanyDescription(" tests/ src/ | grep -v "class CompanyDescription"`
Expected: only the three updated sites in test_enrich_companies.py.

- [ ] **Step 4: Write the failing reaction tests** (append to `tests/test_enrich_companies.py`; follow the file's existing monkeypatch idiom — it patches `complete_json` with an `AsyncMock` and uses the `db` fixture + `_make_company`/`_make_raw_page` helpers):

```python
@pytest.mark.asyncio
async def test_parked_site_clears_website_and_pages(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company(name="Ninegag", slug="ninegag-parked")
    company.website = "https://ninegag.ai"
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://ninegag.ai/"))
    await db.commit()

    canned = CompanyDescription(
        description_short="The domain ninegag.ai is listed for sale.",
        description_long="Parked page; no product information.",
        primary_category="unknown",
        website_state="parked_or_for_sale",
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_enrich_companies(db)
    assert summary.skipped_bad_website == 1
    assert summary.companies_enriched == 0

    await db.refresh(company)
    assert company.website is None
    assert company.website_resolved_at is None
    assert company.rejected_urls == ["https://ninegag.ai"]
    assert company.description_short is None  # junk prose NOT published
    pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == company.id))
    ).scalars().all()
    assert pages == []  # junk pages dropped so the selection stops re-picking


@pytest.mark.asyncio
async def test_not_startup_judgment_excludes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company(name="Old Enterprise", slug="old-enterprise")
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://old.example/"))
    await db.commit()

    canned = CompanyDescription(
        description_short="A 26-year-old customer-service software vendor.",
        description_long="Long text.",
        primary_category="vertical SaaS",
        website_state="ok",
        is_startup=False,
        not_startup_reason="Founded in 2000; publicly traded.",
        founded_year=2000,
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_enrich_companies(db)
    assert summary.companies_enriched == 1
    assert summary.companies_excluded == 1

    await db.refresh(company)
    assert company.exclusion_reason == "not_a_startup"
    assert company.exclusion_detail == "Founded in 2000; publicly traded."
    assert company.excluded_at is not None
    assert company.eligibility_checked_at is not None
    assert company.year_incorporated == 2000
    # Description IS stored (audit), exclusion just hides it from the catalog.
    assert company.description_short is not None


@pytest.mark.asyncio
async def test_non_us_judgment_excludes(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company(name="Bangalore Co", slug="bangalore-co")
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://bangalore.example/"))
    await db.commit()

    canned = CompanyDescription(
        description_short="An Indian HR software company.",
        description_long="Long text.",
        primary_category="vertical SaaS",
        website_state="ok",
        is_startup=True,
        hq_country="IN",
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    await run_enrich_companies(db)
    await db.refresh(company)
    assert company.exclusion_reason == "non_us"
    assert company.hq_country == "IN"


@pytest.mark.asyncio
async def test_ok_startup_sets_stamp_without_exclusion(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    company = _make_company(name="Fine Startup", slug="fine-startup")
    db.add(company)
    await db.flush()
    db.add(_make_raw_page(company.id, url="https://fine.example/"))
    await db.commit()

    canned = CompanyDescription(
        description_short="A developer tools startup.",
        description_long="Long text.",
        primary_category="developer tools",
        website_state="ok",
        is_startup=None,  # unknown → keep
    )
    monkeypatch.setattr(
        "nous.pipeline.enrich_companies.complete_json",
        AsyncMock(return_value=canned),
    )

    await run_enrich_companies(db)
    await db.refresh(company)
    assert company.exclusion_reason is None
    assert company.eligibility_checked_at is not None
```

- [ ] **Step 5: Run to verify failures**

Run: `uv run pytest tests/test_enrich_companies.py -v`
Expected: the 4 new tests FAIL (`skipped_bad_website`/`companies_excluded` don't exist; no exclusion written); pre-existing tests PASS (canned fixtures updated in Step 3).

- [ ] **Step 6: Implement the reactions** in `pipeline/src/nous/pipeline/enrich_companies.py`.

Add `delete` to the sqlalchemy import. Extend `EnrichSummary`:

```python
class EnrichSummary(BaseModel):
    companies_seen: int = 0
    companies_enriched: int = 0
    companies_excluded: int = 0
    people_written: int = 0
    llm_failures: int = 0
    skipped_no_text: int = 0
    skipped_bad_website: int = 0
    skipped_rate_limited: int = 0
```

Add the exclusion skip to the selection (inside the existing `stmt = select(Company)...`):

```python
        .where(Company.exclusion_reason.is_(None))
```

Immediately after the `description: CompanyDescription = await complete_json(...)` try/except block succeeds, insert the bad-website short-circuit (BEFORE the tag-normalization/write block):

```python
        if description.website_state != "ok":
            # The scraped site is parked/for-sale, unrelated, or contentless —
            # the URL is wrong or worthless, which says nothing about the
            # company itself. Reject the URL, clear the website, and drop the
            # junk pages so the selection stops re-picking this company until
            # a new site is resolved + scraped. Junk prose is never published.
            logger.info(
                "Company %s website_state=%s — clearing website %s",
                company.name,
                description.website_state,
                company.website,
            )
            if company.website:
                company.rejected_urls = [
                    *(company.rejected_urls or []),
                    company.website,
                ]
            company.website = None
            company.website_resolved_at = None
            await session.execute(
                delete(RawPage).where(RawPage.company_id == company.id)
            )
            session.add(company)
            try:
                await session.commit()
            except (StaleDataError, IntegrityError):
                await session.rollback()
                logger.warning(
                    "Company %s disappeared mid-enrich (likely a concurrent"
                    " merge) — skipping.",
                    company.id,
                )
            summary.skipped_bad_website += 1
            continue
```

Then, inside the existing write block (after the `if description.industry and not company.industry_group:` lines and before `session.add(company)`), add:

```python
        # Eligibility judgment (spec 2026-06-12). Runs only on website_state
        # == "ok" — a parked/unrelated page supports no judgment. Unknown
        # (None) keeps the company. The judgment stamp prevents the
        # judge-eligibility backfill from re-visiting this row.
        company.eligibility_checked_at = now
        if description.founded_year and not company.year_incorporated:
            company.year_incorporated = description.founded_year
        llm_country = (description.hq_country or "").strip().upper() or None
        if llm_country:
            company.hq_country = llm_country
        if description.is_startup is False:
            company.exclusion_reason = "not_a_startup"
            company.exclusion_detail = description.not_startup_reason
            company.excluded_at = now
            summary.companies_excluded += 1
        elif llm_country is not None and llm_country != "US":
            company.exclusion_reason = "non_us"
            company.exclusion_detail = f"website states HQ country {llm_country}"
            company.excluded_at = now
            summary.companies_excluded += 1
```

(The existing `if (company.hq_city or company.hq_state) and not company.hq_country:` line can stay — it only fires when hq_country is empty, which the new assignment supersedes when the LLM stated a country.)

- [ ] **Step 7: Run the suite**

Run: `uv run pytest tests/test_enrich_companies.py -v`
Expected: ALL PASS.

- [ ] **Step 8: Commit**

```bash
git add src/nous/llm/prompts/company_description.py src/nous/pipeline/enrich_companies.py tests/test_enrich_companies.py
git commit -m "feat(enrich): structured website_state + startup/country judgment with soft exclusion"
```

---

### Task 6: `judge-eligibility` backfill stage

**Files:**
- Create: `pipeline/src/nous/llm/prompts/company_eligibility.py`
- Create: `pipeline/src/nous/pipeline/judge_eligibility.py`
- Create: `pipeline/tests/test_judge_eligibility.py`
- Modify: `pipeline/src/nous/cli.py`

- [ ] **Step 1: Create the prompt module** `pipeline/src/nous/llm/prompts/company_eligibility.py`:

```python
"""Eligibility-judgment prompt for the judge-eligibility backfill stage.

Input: a company's stored description + scraped site text. Output: the same
is_startup / hq_country / founded_year judgment the enrichment prompt makes,
WITHOUT re-writing descriptions (enrichment is write-once). Used to backfill
companies enriched before the judgment existed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EligibilityJudgment(BaseModel):
    is_startup: bool | None = Field(
        default=None,
        description=(
            "True when this reads like an operating startup: an independent, "
            "private company founded within roughly the last 15 years. False "
            "when it clearly is not (decades-old enterprise, publicly traded, "
            "a subsidiary, a fund, a media site). Null when the text does not "
            "support a confident call — never guess."
        ),
    )
    not_startup_reason: str | None = Field(
        default=None,
        description="One short factual sentence; only when is_startup is false.",
    )
    founded_year: int | None = Field(
        default=None,
        description="Founding year ONLY if the text states it. Null otherwise.",
    )
    hq_country: str | None = Field(
        default=None,
        description=(
            "Headquarters country as a 2-letter ISO code ONLY when the text "
            "clearly states it. Null otherwise — never guess."
        ),
    )


PROMPT_TEMPLATE = """\
You are curating a discovery catalog of US software startups. Decide whether
the company below belongs, based ONLY on the text provided.

Rules:
- `is_startup`: true only for an independent, PRIVATE company founded within
  roughly the last 15 years. False for decades-old enterprises, publicly
  traded companies, subsidiaries, funds, or media properties. If the text
  does not support a confident call, return null. Never guess.
- `not_startup_reason`: one short factual sentence, only when is_startup is
  false (e.g. "Founded in 2000; publicly traded enterprise").
- `founded_year` / `hq_country`: only when the text states them. `hq_country`
  is a 2-letter ISO code (e.g. 'US', 'IN', 'GB'). Null otherwise — never
  fabricate.

Company name: {company_name}

Stored description:
---
{description}
---

Website text (may be truncated):
---
{cleaned_text}
---

Return JSON only.
"""


def build_prompt(
    *, company_name: str, description: str, cleaned_text: str
) -> str:
    return PROMPT_TEMPLATE.format(
        company_name=company_name,
        description=description,
        cleaned_text=cleaned_text,
    )
```

- [ ] **Step 2: Write the failing stage tests** — create `tests/test_judge_eligibility.py`:

```python
"""Integration tests for the judge-eligibility backfill stage.

complete_json is monkeypatched; requires DATABASE_URL (same gating as the
other DB suites).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.llm.prompts.company_eligibility import EligibilityJudgment
from nous.pipeline.judge_eligibility import run_judge_eligibility

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _enriched_company(name: str, slug: str) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=slug.replace("-", " "),
        hq_country="US",
        description_short="Does things.",
        description_long="Does many things.",
        last_enriched_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_judgment_excludes_and_stamps(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_co = _enriched_company("Old Enterprise", "old-enterprise-judge")
    db.add(old_co)
    await db.flush()
    db.add(
        RawPage(
            company_id=old_co.id,
            url="https://old.example/",
            content="Serving the enterprise since 2000." * 20,
        )
    )
    await db.commit()

    canned = EligibilityJudgment(
        is_startup=False,
        not_startup_reason="Founded in 2000; publicly traded.",
        founded_year=2000,
    )
    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=canned),
    )

    summary = await run_judge_eligibility(db)
    assert summary.companies_judged == 1
    assert summary.companies_excluded == 1

    await db.refresh(old_co)
    assert old_co.exclusion_reason == "not_a_startup"
    assert old_co.eligibility_checked_at is not None
    assert old_co.year_incorporated == 2000

    # Second run selects nothing — the stamp makes the backfill one-shot.
    summary2 = await run_judge_eligibility(db)
    assert summary2.companies_judged == 0


@pytest.mark.asyncio
async def test_unknown_keeps_company(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    co = _enriched_company("Fine Co", "fine-co-judge")
    db.add(co)
    await db.commit()

    monkeypatch.setattr(
        "nous.pipeline.judge_eligibility.complete_json",
        AsyncMock(return_value=EligibilityJudgment()),
    )

    summary = await run_judge_eligibility(db)
    assert summary.companies_judged == 1
    assert summary.companies_excluded == 0
    await db.refresh(co)
    assert co.exclusion_reason is None
    assert co.eligibility_checked_at is not None
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_judge_eligibility.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nous.pipeline.judge_eligibility'`.

- [ ] **Step 4: Implement the stage** — create `pipeline/src/nous/pipeline/judge_eligibility.py`:

```python
"""judge-eligibility pipeline stage (one-time backfill, safe to keep running).

Runs the is-this-a-startup judgment over companies that were enriched BEFORE
enrich-companies started making it (description present, eligibility never
checked). Reads stored raw_pages text; never re-writes descriptions.

Commit cadence: one commit per company. Rate-limit handling: stop the loop on
LLMRateLimitError (same pattern as enrich-companies). Selection is stamped via
eligibility_checked_at, so bounded daily runs drain the backlog and steady
state selects nothing (new enrichments stamp themselves).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from nous.db.models import Company, RawPage
from nous.llm.client import (
    MAX_PROMPT_INPUT_CHARS,
    LLMError,
    LLMParseError,
    LLMRateLimitError,
    complete_json,
)
from nous.llm.prompts.company_eligibility import EligibilityJudgment, build_prompt
from nous.util.text import extract_visible_text, truncate_to_chars

logger = logging.getLogger(__name__)


class JudgeEligibilitySummary(BaseModel):
    companies_judged: int = 0
    companies_excluded: int = 0
    llm_failures: int = 0
    skipped_rate_limited: int = 0


async def run_judge_eligibility(
    session: AsyncSession,
    *,
    limit: int | None = None,
) -> JudgeEligibilitySummary:
    summary = JudgeEligibilitySummary()

    stmt = (
        select(Company)
        .where(Company.description_short.is_not(None))
        .where(Company.eligibility_checked_at.is_(None))
        .where(Company.exclusion_reason.is_(None))
        .order_by(Company.name.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    companies = (await session.execute(stmt)).scalars().all()

    for company in companies:
        pages = (
            await session.execute(
                select(RawPage)
                .where(RawPage.company_id == company.id)
                .order_by(RawPage.url.asc())
            )
        ).scalars().all()
        parts = [extract_visible_text(p.content) for p in pages]
        cleaned = truncate_to_chars(
            "\n\n".join(p for p in parts if p), MAX_PROMPT_INPUT_CHARS
        )

        prompt = build_prompt(
            company_name=company.name,
            description=company.description_short or "",
            cleaned_text=cleaned or "(no scraped text on record)",
        )

        try:
            judgment: EligibilityJudgment = await complete_json(
                prompt, EligibilityJudgment
            )
        except LLMRateLimitError as exc:
            logger.warning(
                "LLM rate limit hit while judging %s — stopping loop. Raw: %s",
                company.name,
                exc,
            )
            summary.skipped_rate_limited += 1
            break
        except (LLMParseError, LLMError) as exc:
            logger.warning("LLM error judging %s: %s", company.name, exc)
            summary.llm_failures += 1
            continue

        now = datetime.now(tz=UTC)
        company.eligibility_checked_at = now
        if judgment.founded_year and not company.year_incorporated:
            company.year_incorporated = judgment.founded_year
        llm_country = (judgment.hq_country or "").strip().upper() or None
        if llm_country:
            company.hq_country = llm_country
        if judgment.is_startup is False:
            company.exclusion_reason = "not_a_startup"
            company.exclusion_detail = judgment.not_startup_reason
            company.excluded_at = now
            summary.companies_excluded += 1
        elif llm_country is not None and llm_country != "US":
            company.exclusion_reason = "non_us"
            company.exclusion_detail = f"website states HQ country {llm_country}"
            company.excluded_at = now
            summary.companies_excluded += 1

        session.add(company)
        try:
            await session.commit()
        except (StaleDataError, IntegrityError):
            await session.rollback()
            logger.warning(
                "Company %s disappeared mid-judge (likely a concurrent merge)"
                " — skipping.",
                company.id,
            )
            summary.llm_failures += 1
            continue
        summary.companies_judged += 1

    return summary
```

- [ ] **Step 5: Register the CLI command** — append to `pipeline/src/nous/cli.py` (before `def main()`), matching the house style:

```python
@cli.command("judge-eligibility")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of companies to judge (caps LLM spend per run).",
)
def judge_eligibility(limit: int | None) -> None:
    """Backfill the is-this-a-startup judgment for already-enriched companies."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.observability import emit_run_telemetry
    from nous.pipeline.judge_eligibility import run_judge_eligibility

    async def _run() -> None:
        try:
            async with AsyncSessionLocal() as session:
                summary = await run_judge_eligibility(session, limit=limit)
                click.echo(summary.model_dump_json(indent=2))
        finally:
            emit_run_telemetry("judge-eligibility")

    asyncio.run(_run())
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_judge_eligibility.py -v && uv run nous judge-eligibility --help`
Expected: tests PASS; help text prints.

- [ ] **Step 7: Commit**

```bash
git add src/nous/llm/prompts/company_eligibility.py src/nous/pipeline/judge_eligibility.py tests/test_judge_eligibility.py src/nous/cli.py
git commit -m "feat(pipeline): judge-eligibility backfill stage for pre-existing enriched companies"
```

---

### Task 7: `repair-catalog` one-time stage

**Files:**
- Create: `pipeline/src/nous/pipeline/repair_catalog.py`
- Create: `pipeline/tests/test_repair_catalog.py`
- Modify: `pipeline/src/nous/cli.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_repair_catalog.py`:

```python
"""Integration tests for the one-time repair-catalog stage.

Covers: suffix rename, collision-merge, LSIP husk delete, LSIP-with-data
exclude, parked-description reset, SellRaze-style false-positive safety,
and run-twice idempotency. Requires DATABASE_URL.
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, RawPage
from nous.pipeline.repair_catalog import run_repair_catalog

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


@pytest.mark.asyncio
async def test_both_funds_suffix_renamed(db: AsyncSession) -> None:
    co = _co("1047 gamesLSVP and LSIP Investment", "1047-gameslsvp-and-lsip-investment")
    db.add(co)
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.names_cleaned == 1

    await db.refresh(co)
    assert co.name == "1047 games"
    assert co.normalized_name == "1047 games"
    assert co.slug == "1047-games"
    assert co.exclusion_reason is None


@pytest.mark.asyncio
async def test_both_funds_collision_merges_into_existing(db: AsyncSession) -> None:
    clean = _co("Composio", "composio", description_short="Tool-use platform.")
    suffixed = _co("ComposioLSVP and LSIP Investment", "composiolsvp-and-lsip-investment")
    db.add_all([clean, suffixed])
    await db.commit()
    suffixed_id = suffixed.id

    summary = await run_repair_catalog(db)
    assert summary.merged == 1

    gone = (
        await db.execute(select(Company).where(Company.id == suffixed_id))
    ).scalar_one_or_none()
    assert gone is None
    survivor = (
        await db.execute(select(Company).where(Company.slug == "composio"))
    ).scalar_one()
    assert survivor.description_short == "Tool-use platform."


@pytest.mark.asyncio
async def test_lsip_husk_deleted_but_linked_row_excluded(db: AsyncSession) -> None:
    husk = _co("ApnaLSIP Investment", "apnalsip-investment")
    funded = _co("AckoLSIP Investment", "ackolsip-investment")
    db.add_all([husk, funded])
    await db.flush()
    db.add(
        FundingRound(
            company_id=funded.id, round_type="Series D", announced_date=date(2024, 1, 1)
        )
    )
    await db.commit()
    husk_id = husk.id

    summary = await run_repair_catalog(db)
    assert summary.lsip_deleted == 1
    assert summary.lsip_excluded == 1

    assert (
        await db.execute(select(Company).where(Company.id == husk_id))
    ).scalar_one_or_none() is None
    await db.refresh(funded)
    assert funded.exclusion_reason == "non_us"
    assert funded.name == "Acko"  # name still cleaned on the kept row


@pytest.mark.asyncio
async def test_parked_description_reset(db: AsyncSession) -> None:
    parked = _co(
        "Ninegag",
        "ninegag-repair",
        website="https://ninegag.ai",
        description_short=(
            "The domain ninegag.ai is listed for sale on Spaceship.com; no "
            "product or company information is available."
        ),
        description_long="Parked.",
    )
    # Real company whose copy mentions selling — must NOT be touched.
    sellraze = _co(
        "SellRaze",
        "sellraze-repair",
        website="https://sellraze.com",
        description_short=(
            "SellRaze lets sellers list items for sale across marketplaces "
            "using image recognition."
        ),
    )
    db.add_all([parked, sellraze])
    await db.flush()
    db.add(RawPage(company_id=parked.id, url="https://ninegag.ai/", content="x" * 300))
    await db.commit()

    summary = await run_repair_catalog(db)
    assert summary.parked_reset == 1

    await db.refresh(parked)
    assert parked.website is None
    assert parked.website_resolved_at is None
    assert parked.description_short is None
    assert parked.description_long is None
    assert parked.rejected_urls == ["https://ninegag.ai"]
    pages = (
        await db.execute(select(RawPage).where(RawPage.company_id == parked.id))
    ).scalars().all()
    assert pages == []

    await db.refresh(sellraze)
    assert sellraze.description_short is not None
    assert sellraze.website == "https://sellraze.com"


@pytest.mark.asyncio
async def test_repair_is_idempotent(db: AsyncSession) -> None:
    db.add(_co("FoxyLSIP Investment", "foxylsip-investment"))
    await db.commit()

    first = await run_repair_catalog(db)
    assert first.lsip_deleted == 1
    second = await run_repair_catalog(db)
    assert (
        second.names_cleaned
        == second.lsip_deleted
        == second.lsip_excluded
        == second.merged
        == second.parked_reset
        == 0
    )


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db: AsyncSession) -> None:
    co = _co("AstroLSVP and LSIP Investment", "astrolsvp-and-lsip-investment")
    db.add(co)
    await db.commit()

    summary = await run_repair_catalog(db, dry_run=True)
    assert summary.names_cleaned == 1  # counted as would-do

    await db.refresh(co)
    assert co.name == "AstroLSVP and LSIP Investment"  # unchanged
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_repair_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nous.pipeline.repair_catalog'`.

- [ ] **Step 3: Implement the stage** — create `pipeline/src/nous/pipeline/repair_catalog.py`:

```python
"""repair-catalog pipeline stage — one-time data repair, idempotent.

Two repairs (spec 2026-06-12 §3):

1. Lightspeed badge-suffix names ("...LSVP and LSIP Investment" /
   "...LSIP Investment", 96 prod rows): strip the suffix; LSIP-only rows are
   Lightspeed-India holdings (out of scope) — DELETE when they are husks
   (no funding rounds, no news), soft-exclude as 'non_us' when they have
   accrued data. Renames that collide with an existing clean-named row merge
   into it via the dedup machinery.

2. Parked-domain enrichments (~30 prod rows): rows whose description matches
   conservative domain-sale prose patterns get their website + descriptions
   cleared, the bad URL recorded in rejected_urls, and their raw_pages
   dropped, so resolve/scrape/enrich start over cleanly.

Idempotent: pass 1 leaves no suffixed names; pass 2 clears the descriptions
it matches on. A second run selects nothing. ``--dry-run`` logs intended
actions without writing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FundingRound, NewsArticle, RawPage
from nous.db.upsert import _build_slug, _find_by_normalized_name, merge_companies
from nous.util.slugify import normalize_name

logger = logging.getLogger(__name__)

_BOTH_SUFFIX = "LSVP and LSIP Investment"
_LSIP_SUFFIX = "LSIP Investment"

# Conservative domain-sale prose patterns (matched against description_short).
# Deliberately requires domain-sale wording — bare "for sale" false-matched
# real product copy (SellRaze) in the prod analysis. Rows these miss (wrong
# but live sites, launching-soon pages) are left for judge-eligibility /
# manual exclusion; see the spec's repair section.
_PARKED_DESC_PATTERNS: tuple[str, ...] = (
    "%domain%for sale%",
    "%for sale%domain%",
    "%parking page%",
    "%parked%",
    "%domain marketplace%",
    "%placeholder%for sale%",
)


class RepairSummary(BaseModel):
    names_cleaned: int = 0
    merged: int = 0
    lsip_deleted: int = 0
    lsip_excluded: int = 0
    parked_reset: int = 0
    dry_run: bool = False


async def _has_any(
    session: AsyncSession, model: type[FundingRound] | type[NewsArticle], company_id: object
) -> bool:
    row = (
        await session.execute(
            select(model.id).where(model.company_id == company_id).limit(1)
        )
    ).first()
    return row is not None


async def run_repair_catalog(
    session: AsyncSession, *, dry_run: bool = False
) -> RepairSummary:
    summary = RepairSummary(dry_run=dry_run)
    now = datetime.now(tz=UTC)

    # ── Pass 1: Lightspeed badge suffixes ────────────────────────────────────
    suffixed = (
        (
            await session.execute(
                select(Company).where(
                    or_(
                        Company.name.like(f"%{_LSIP_SUFFIX}"),
                        Company.name.like("%LSVP Investment"),
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    for company in suffixed:
        is_both = company.name.endswith(_BOTH_SUFFIX)
        suffix = _BOTH_SUFFIX if is_both else _LSIP_SUFFIX
        clean_name = company.name.removesuffix(suffix).strip()

        if not clean_name or not is_both:
            # LSIP-only (or a name that is nothing but the badge): India
            # portfolio — out of scope. Delete husks; the fixed adapter never
            # re-emits them. Keep + exclude rows that accrued real data.
            has_data = await _has_any(
                session, FundingRound, company.id
            ) or await _has_any(session, NewsArticle, company.id)
            if not has_data:
                logger.info("repair: deleting LSIP husk %r", company.name)
                summary.lsip_deleted += 1
                if not dry_run:
                    await session.delete(company)
                continue
            logger.info("repair: excluding LSIP row with data %r", company.name)
            summary.lsip_excluded += 1
            if not dry_run and clean_name:
                await _rename(session, company, clean_name)
            if not dry_run:
                company.exclusion_reason = "non_us"
                company.exclusion_detail = "Lightspeed India portfolio entry"
                company.excluded_at = now
                session.add(company)
            continue

        # Both-funds row: keep, clean the name; merge on collision.
        existing = await _find_by_normalized_name(session, normalize_name(clean_name))
        if existing is not None and existing.id != company.id:
            logger.info(
                "repair: merging %r into existing %r", company.name, existing.name
            )
            summary.merged += 1
            if not dry_run:
                await merge_companies(
                    session, survivor_id=existing.id, loser_id=company.id
                )
            continue

        logger.info("repair: renaming %r -> %r", company.name, clean_name)
        summary.names_cleaned += 1
        if not dry_run:
            await _rename(session, company, clean_name)
            session.add(company)

    if not dry_run:
        await session.commit()

    # ── Pass 2: parked-domain enrichments ────────────────────────────────────
    parked = (
        (
            await session.execute(
                select(Company).where(
                    Company.website.is_not(None),
                    or_(
                        *[
                            Company.description_short.ilike(p)
                            for p in _PARKED_DESC_PATTERNS
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )

    for company in parked:
        logger.info(
            "repair: resetting parked row %r (website %s; desc %r)",
            company.name,
            company.website,
            (company.description_short or "")[:80],
        )
        summary.parked_reset += 1
        if dry_run:
            continue
        if company.website:
            company.rejected_urls = [*(company.rejected_urls or []), company.website]
        company.website = None
        company.website_resolved_at = None
        company.description_short = None
        company.description_long = None
        company.primary_category = None
        company.tags = None
        company.last_enriched_at = None
        company.last_enriched_payload = None
        company.eligibility_checked_at = None
        await session.execute(
            delete(RawPage).where(RawPage.company_id == company.id)
        )
        session.add(company)

    if not dry_run:
        await session.commit()

    return summary


async def _rename(session: AsyncSession, company: Company, clean_name: str) -> None:
    """Apply a cleaned display name + regenerated identity fields in place."""
    company.name = clean_name
    company.normalized_name = normalize_name(clean_name)
    company.slug = await _build_slug(
        session, clean_name, company.id, company.website
    )
```

NOTE for the implementer: `_build_slug` and `_find_by_normalized_name` are
private helpers in `nous.db.upsert` — importing them here is intentional
(same package family); if ruff complains about private-member import, promote
them by removing the leading underscore in `upsert.py` and updating its
internal references (3 call sites) instead of duplicating logic.

- [ ] **Step 4: Register the CLI command** — append to `pipeline/src/nous/cli.py`:

```python
@cli.command("repair-catalog")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log intended repairs without writing.",
)
def repair_catalog(dry_run: bool) -> None:
    """One-time catalog repair: Lightspeed badge-suffix names + parked-domain rows."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.repair_catalog import run_repair_catalog

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            summary = await run_repair_catalog(session, dry_run=dry_run)
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_repair_catalog.py -v`
Expected: 6 PASSED. (If the f-string `f"%LSVP Investment"` trips ruff F541, drop the `f` prefix.)

- [ ] **Step 6: Commit**

```bash
git add src/nous/pipeline/repair_catalog.py tests/test_repair_catalog.py src/nous/cli.py
git commit -m "feat(pipeline): idempotent repair-catalog stage for Lightspeed names + parked rows"
```

---

### Task 8: Pipeline stages skip excluded rows + manual lever

**Files:**
- Modify (one-line where-clauses): `pipeline/src/nous/pipeline/resolve_homepages.py` (~line 73), `scrape_homepages.py` (~line 320 stmt), `ingest_news.py` (~line 125 company_stmt), `extract_funding.py` (`run_extract_funding_website` stmt ~line 461), `estimate_employees.py` (~line 73), `analyze_competitors.py` (~line 97)
- Create: `pipeline/src/nous/pipeline/exclude_company.py`
- Modify: `pipeline/src/nous/cli.py`
- Modify: `pipeline/tests/test_resolve_homepages.py`, `pipeline/tests/test_auto_create.py`

- [ ] **Step 1: Write the failing selection tests.**

Append to `tests/test_resolve_homepages.py` (reuse its company-factory conventions):

```python
async def test_excluded_companies_not_selected_for_resolve(db: AsyncSession) -> None:
    """An excluded company is never picked up by the resolve selection, even
    when it is otherwise eligible (no website, never attempted)."""
    excluded = _make_company(name="Excluded Co", slug="excluded-co-resolve")
    excluded.exclusion_reason = "not_a_startup"
    db.add(excluded)
    await db.flush()
    await db.commit()

    summary = await run_resolve_homepages(db, MockHomepageClient({}))
    assert summary.companies_seen == 0
```

Append to `tests/test_auto_create.py`:

```python
@pytest.mark.asyncio
async def test_rediscovery_never_clears_exclusion(db: AsyncSession) -> None:
    excluded = Company(
        name="Acko",
        slug="acko-excluded",
        normalized_name=normalize_name("Acko"),
        hq_country="US",
        exclusion_reason="non_us",
        exclusion_detail="Lightspeed India portfolio entry",
    )
    db.add(excluded)
    await db.commit()

    company, created = await auto_create_company(
        db, name="Acko", website=None, discovered_via="vc_portfolio"
    )
    assert created is False
    assert company.id == excluded.id
    assert company.exclusion_reason == "non_us"  # re-listing is not new evidence
```

- [ ] **Step 2: Run to verify the resolve test fails**

Run: `uv run pytest tests/test_resolve_homepages.py -k excluded -v`
Expected: FAIL — `companies_seen == 1` (excluded row was selected). The auto_create test passes already (nothing writes exclusion_reason) — it's a regression guard; keep it.

- [ ] **Step 3: Add the where-clauses.** In each file, add one line to the company selection:

`resolve_homepages.py` (line ~73):
```python
    stmt = select(Company).where(
        Company.website.is_(None),
        Company.exclusion_reason.is_(None),
        or_(
            Company.website_resolved_at.is_(None),
            Company.website_resolved_at < cutoff,
        ),
    )
```

`scrape_homepages.py` — in the `stmt = (select(Company)...` chain, after `.where(Company.website.is_not(None))`:
```python
        .where(Company.exclusion_reason.is_(None))
```

`ingest_news.py` (line ~125):
```python
    company_stmt = (
        select(Company)
        .where(Company.exclusion_reason.is_(None))
        .order_by(Company.news_checked_at.asc().nulls_first())
    )
```

`extract_funding.py` — in `run_extract_funding_website`'s stmt, after the two `exists()` wheres:
```python
        .where(Company.exclusion_reason.is_(None))
```

`estimate_employees.py` — add to the existing `select(Company).where(...)` conditions:
```python
        Company.exclusion_reason.is_(None),
```

`analyze_competitors.py` — same one-liner in its `select(Company)` eligibility chain (line ~97):
```python
        .where(Company.exclusion_reason.is_(None))
```

(`enrich_companies.py` already got its clause in Task 5. Read each chain before editing — the clause composes with AND in all six spots; mypy will catch misplacement.)

- [ ] **Step 4: Implement the manual lever** — create `pipeline/src/nous/pipeline/exclude_company.py`:

```python
"""exclude-company helper — the manual lever behind the CLI command.

Lets the operator exclude (or re-include) a single company by slug without
raw SQL, e.g. junk the automated rules missed. Reason 'manual' by default.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company

VALID_REASONS = ("parse_artifact", "non_us", "not_a_startup", "manual")


class ExcludeResult(BaseModel):
    slug: str
    found: bool
    exclusion_reason: str | None = None


async def run_exclude_company(
    session: AsyncSession,
    *,
    slug: str,
    reason: str = "manual",
    detail: str | None = None,
    clear: bool = False,
) -> ExcludeResult:
    if not clear and reason not in VALID_REASONS:
        raise ValueError(f"reason must be one of {VALID_REASONS}, got {reason!r}")

    company = (
        await session.execute(select(Company).where(Company.slug == slug))
    ).scalar_one_or_none()
    if company is None:
        return ExcludeResult(slug=slug, found=False)

    if clear:
        company.exclusion_reason = None
        company.exclusion_detail = None
        company.excluded_at = None
    else:
        company.exclusion_reason = reason
        company.exclusion_detail = detail
        company.excluded_at = datetime.now(tz=UTC)
    session.add(company)
    await session.commit()
    return ExcludeResult(
        slug=slug, found=True, exclusion_reason=company.exclusion_reason
    )
```

CLI command (append to `cli.py`):

```python
@cli.command("exclude-company")
@click.argument("slug")
@click.option(
    "--reason",
    type=click.Choice(["parse_artifact", "non_us", "not_a_startup", "manual"]),
    default="manual",
    show_default=True,
    help="Recorded exclusion reason.",
)
@click.option("--detail", type=str, default=None, help="Free-form audit note.")
@click.option(
    "--clear",
    is_flag=True,
    default=False,
    help="Re-include the company (clears the exclusion).",
)
def exclude_company(slug: str, reason: str, detail: str | None, clear: bool) -> None:
    """Manually exclude (or --clear) a company from the catalog by slug."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.exclude_company import run_exclude_company

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            result = await run_exclude_company(
                session, slug=slug, reason=reason, detail=detail, clear=clear
            )
            click.echo(result.model_dump_json(indent=2))

    asyncio.run(_run())
```

- [ ] **Step 5: Run the affected suites**

Run: `uv run pytest tests/test_resolve_homepages.py tests/test_auto_create.py tests/test_scrape_homepages.py tests/test_ingest_news.py tests/test_extract_funding.py tests/test_estimate_employees_stage.py tests/test_analyze_competitors_stage.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nous/pipeline/ tests/test_resolve_homepages.py tests/test_auto_create.py src/nous/cli.py
git commit -m "feat(pipeline): excluded companies skip every per-company stage; add exclude-company CLI"
```

---

### Task 9: Web — catalog bar + exclusion everywhere

**Files:**
- Modify: `web/lib/types.ts` (CompanyRow)
- Modify: `web/lib/queries.ts`
- Modify: `web/lib/spotlight.ts`

Pre-step: skim `web/node_modules/next/dist/docs/` only if route files end up touched — these edits are lib-only. Pre-migration prod gracefully 400s→empty on the new columns (documented precedent: `getCompanyOgData`/0021), so no compatibility shims.

- [ ] **Step 1: Types** — in `web/lib/types.ts`, append to `CompanyRow` (after the `consecutive_scrape_failures` field, matching the optional-column idiom documented on `total_raised_usd`):

```ts
  // Catalog-quality soft exclusion (migration 0022). NULL/undefined = included.
  // Optional (`?`), not just nullable: prod rows lack the column until the
  // migration runs there; select("*") omits unknown columns. Treat undefined
  // as null. A non-null value means the company page must 404.
  exclusion_reason?: string | null;
  // Denormalized count(funding_rounds) (migration 0022) backing the catalog
  // bar. Same optionality caveat as above.
  funding_round_count?: number | null;
```

- [ ] **Step 2: The catalog bar in `queries.ts`.** Add near the top (after `sanitizeIlikeTerm`):

```ts
/**
 * Catalog bar (spec 2026-06-12): a company is publicly listed iff it is not
 * excluded AND (it has a description OR ≥1 recorded funding round). Companies
 * failing the bar stay in the DB and reappear once the pipeline learns
 * something about them. Apply via:
 *   query.is("exclusion_reason", null).or(CATALOG_BAR_OR)
 * PostgREST ANDs the .or() group with every other filter.
 */
const CATALOG_BAR_OR =
  "description_short.not.is.null,funding_round_count.gt.0";
```

Then apply both calls — `.is("exclusion_reason", null).or(CATALOG_BAR_OR)` — to each catalog-facing query right after its `.select(...)`:

1. `listCompanies` (line ~116):
```ts
  let query = supabase
    .from("companies")
    .select(
      "slug, name, hq_city, hq_state, industry_group, description_short, status",
      { count: "exact" },
    )
    .is("exclusion_reason", null)
    .or(CATALOG_BAR_OR);
```
2. `listNewestCompanies` — same two lines after `.select("slug, name, description_short")`.
3. `countCompanies` — after `.select("id", { count: "exact", head: true })`.
4. `getRandomCompanySlug` — BOTH queries (the count and the offset row fetch must see the same set).
5. `listNewThisWeekCompanies` — after its `.select(...)`.
6. `countNewThisWeek` — the companies count only (rounds count unchanged).

- [ ] **Step 3: scanTable gains `catalogOnly`** (drives the dropdowns/sitemap/tag/state scans):

```ts
async function scanTable(
  table: "companies" | "investors",
  label: string,
  select: string,
  notNullColumn?: string,
  catalogOnly = false,
): Promise<TableScanResult> {
```
…and inside the page loop, after the `notNullColumn` block:
```ts
    if (catalogOnly) {
      query = query.is("exclusion_reason", null).or(CATALOG_BAR_OR);
    }
```
`scanCompanies` passes it through:
```ts
async function scanCompanies(
  label: string,
  select: string,
  notNullColumn?: string,
  catalogOnly = false,
): Promise<Record<string, unknown>[] | null> {
  const { rows, ok } = await scanTable(
    "companies", label, select, notNullColumn, catalogOnly,
  );
  return ok ? rows : null;
}
```
Update callers to `catalogOnly = true`: `listIndustryGroups`, `getIndustrySummary`, `listAllTags`, `listAllStates`, `listAllCompanySlugs` (the sitemap must not advertise unlisted pages). Investor scans (`scanTable("investors", ...)`) stay untouched.

- [ ] **Step 4: Excluded companies 404 + joins drop excluded rows.**

`getCompanyBySlug` — after the `if (companyError || !company)` guard:
```ts
  // Excluded companies 404 (spec 2026-06-12) — junk pages must not render
  // even by direct URL. Hidden-but-legit husks (no exclusion) still render.
  if ((company as { exclusion_reason?: string | null }).exclusion_reason) {
    return null;
  }
```

`getCompanyOgData` — add `exclusion_reason` to the select string and return null when set:
```ts
    .select(
      "name, industry_group, exclusion_reason, total_raised_usd, funding_rounds(amount_raised)",
    )
```
…and after the error guard:
```ts
  if ((company as { exclusion_reason?: string | null }).exclusion_reason) {
    return null;
  }
```

`listRecentFundings` and `listNewThisWeekFundingRounds` — drop rounds whose company is excluded by switching the embed to an inner join + filter:
```ts
    .select("round_type, amount_raised, announced_date, companies!inner(name, slug)")
    .is("companies.exclusion_reason", null)
```
(respectively `"round_type, amount_raised, announced_date, created_at, companies!inner(slug, name)"` for the new-this-week variant; keep the rest of each query unchanged — the client-side missing-company drop still stands).

`getCompanyBySlug` competitors join — excluded competitors degrade to plain text: change the select to
```ts
        .select("*, competitor_company:companies!competitor_company_id(slug, name, exclusion_reason)")
```
and in the mapping:
```ts
    const resolved =
      nested && nested.slug && nested.name && !nested.exclusion_reason
        ? { slug: nested.slug, name: nested.name }
        : null;
```
(extend `NestedResolvedCompany` with `exclusion_reason?: string | null`).

`getInvestorBySlug` portfolio — add `exclusion_reason` to the embedded companies select and drop excluded rows in the flatMap:
```ts
      .select(
        "companies(slug, name, hq_city, hq_state, industry_group, description_short, status, exclusion_reason)",
      )
```
…in `PortfolioJoin`'s nested type add `exclusion_reason?: string | null;`, and in the flatMap:
```ts
      if (!c?.slug || !c.name || c.exclusion_reason) return [];
```
Same treatment for the rounds query's nested companies: select `companies(slug, name, exclusion_reason)` and `if (!c?.slug || !c.name || c.exclusion_reason) return [];` (extend the inline type accordingly).

- [ ] **Step 5: Spotlight** — in `web/lib/spotlight.ts`, the two company display/candidate fetches (lines ~400 and ~441, the ones with `.not("description_short", "is", null)` + `.eq("status", "active")`) each gain:

```ts
      .is("exclusion_reason", null)
```

- [ ] **Step 6: Build**

Run: `cd web && npm run build`
Expected: build succeeds (typecheck included). Fix any type errors at their site — do not loosen types.

- [ ] **Step 7: Commit**

```bash
git add web/lib/types.ts web/lib/queries.ts web/lib/spotlight.ts
git commit -m "feat(web): catalog bar — hide excluded + contentless companies across all queries"
```

---

### Task 10: Workflow wiring + spec touch-up + full verification

**Files:**
- Modify: `.github/workflows/descriptions.yml`
- Modify: `docs/superpowers/specs/2026-06-12-catalog-quality-filtering-design.md`

- [ ] **Step 1: Add the two steps to `descriptions.yml`.**

Immediately after the `Apply migrations` step:

```yaml
      - name: Repair catalog (idempotent; no-op after first run)
        id: repair
        timeout-minutes: 15
        continue-on-error: true
        run: uv run nous repair-catalog
```

Immediately after the `Enrich companies` step:

```yaml
      - name: Judge eligibility (backfill; drains at 200/day then no-ops)
        id: judge
        timeout-minutes: 30
        continue-on-error: true
        run: uv run nous judge-eligibility --limit 200
```

(Both steps carry ids on purpose: a success should count toward the existing
`contains(steps.*.outcome, 'success')` Vercel-deploy gate.)

- [ ] **Step 2: Fix the stale spec line.** In the spec's §5 backfill paragraph, replace "throttled to fit Gemini free-tier daily limits (spread over a few days; resumable" with "bounded by `--limit 200` per daily run on DeepSeek (~1,600 one-time calls ≈ $1–3; resumable". Also update the repo-root `CLAUDE.md` stack line if it still says Gemini — verify first: `grep -n "Gemini" CLAUDE.md nous-technical-spec.md` and align with what `pipeline/src/nous/config.py` actually configures; if the spec/CLAUDE.md disagree with the code, fix the doc to match the code in a separate sentence, not silently.

- [ ] **Step 3: Full verification gauntlet** (from repo root):

```bash
cd pipeline
export DATABASE_URL=$(grep -h '^DATABASE_URL' .env | cut -d= -f2- | sed 's/^"//;s/"$//')
uv run ruff check . && uv run mypy src && uv run pytest -q
cd ../web && npm run build
```
Expected: every command exits 0. Paste actual outputs in the task report — no green-by-assertion.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/descriptions.yml docs/superpowers/specs/2026-06-12-catalog-quality-filtering-design.md
git commit -m "ci(descriptions): run repair-catalog + judge-eligibility; fix stale spec wording"
```

---

### Task 11: PR + rollout + prod verification

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin catalog-quality-filtering
gh pr create --title "Catalog quality filtering: soft exclusion, Lightspeed fix, parked-domain rejection" --body "$(cat <<'EOF'
Implements docs/superpowers/specs/2026-06-12-catalog-quality-filtering-design.md.

- Lightspeed adapter: fund-badge text no longer bleeds into names (96 prod rows); India-only (LSIP) holdings skipped
- resolve-homepages rejects parked/for-sale pages and previously-rejected domains
- enrich-companies emits structured website_state / is_startup / hq_country and soft-excludes non-startups + non-US
- one-time idempotent repair-catalog stage (runs in descriptions.yml after migrations)
- judge-eligibility backfill for ~1,600 already-enriched companies — one-time DeepSeek cost ≈ $1–3 at --limit 200/day
- web catalog bar: exclusion_reason IS NULL AND (description OR ≥1 round) across list/search/counts/dropdowns/sitemap/spotlight; excluded detail pages 404

Rollout note: until migration 0022 reaches prod, web queries referencing the
new columns 400→degrade to empty (same pattern as 0021/getCompanyOgData);
dispatch the funding-news workflow with both skips right after merge to apply
the migration immediately.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Watch checks, then squash-merge** (repo convention):

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
```

- [ ] **Step 3: Apply the migration + repair to prod immediately** (don't wait for tomorrow's 06:00 cron — the web deploy may land first and serve empty lists until the columns exist):

```bash
gh workflow run funding-news.yml -f skip_news=true -f skip_funding=true   # applies alembic upgrade head
gh run watch                                                              # wait for completion
gh workflow run descriptions.yml -f skip_resolve=true -f skip_scrape=true -f skip_enrich=true  # runs repair-catalog + first judge batch
gh run watch
```

(Runs queue behind any in-progress pipeline via the `nous-pipeline-db` concurrency group — expected.)

- [ ] **Step 4: Verify prod data** (PostgREST, creds from `web/.env.local` as in the investigation):

```bash
cd web
SUPA_URL=$(grep -E '^(NEXT_PUBLIC_)?SUPABASE_URL' .env.local | head -1 | cut -d= -f2-)
SUPA_KEY=$(grep -E 'SERVICE_ROLE' .env.local | head -1 | cut -d= -f2-)
# 0 remaining suffixed names:
curl -sf "$SUPA_URL/rest/v1/companies?select=slug&limit=1&name=ilike.*LSIP%20Investment" -H "apikey: $SUPA_KEY" -H "Authorization: Bearer $SUPA_KEY" -H "Prefer: count=exact" -D - -o /dev/null | grep -i content-range   # expect */0
# 0 remaining parked-prose descriptions with a website:
curl -sf "$SUPA_URL/rest/v1/companies?select=slug&limit=1&website=not.is.null&or=(description_short.ilike.*domain*for%20sale*,description_short.ilike.*parking%20page*)" -H "apikey: $SUPA_KEY" -H "Authorization: Bearer $SUPA_KEY" -H "Prefer: count=exact" -D - -o /dev/null | grep -i content-range   # expect */0
# exclusions exist and are attributed:
curl -sf "$SUPA_URL/rest/v1/companies?select=exclusion_reason&exclusion_reason=not.is.null&limit=5" -H "apikey: $SUPA_KEY" -H "Authorization: Bearer $SUPA_KEY"
```

- [ ] **Step 5: Verify the live site** — `/companies` no longer shows `1047 gamesLSVP...`-style names or name-only husk rows; the 9gag page is gone from listings; `/c/dev-agents` (real-but-husk) still renders by direct URL but is absent from the browse list. Report what you actually see.

---

## Self-review notes (already applied)

- **Spec coverage:** schema §1→Task 1; adapter §2→Task 3; repair §3→Task 7; resolver §4→Task 4; enrichment §5→Tasks 5–6; stage skips + lever §6→Task 8; web §7→Task 9; testing §8→inline; rollout §9→Tasks 10–11. The spec's "~41 parked rows" relaxes to ~30 here: the repair patterns deliberately exclude launching-soon/thin-site rows (possibly-correct domains must not enter `rejected_urls`); judge-eligibility + the manual CLI cover the residue. This is the conservative reading of the spec's own false-positive warning.
- **Type consistency:** `exclusion_reason` values are the same 4 strings in the CHECK, the CLI choices, `VALID_REASONS`, and all writers. `rejected_urls` is always reassigned, never mutated. `refresh_funding_round_count` is the only count writer besides migration backfill.
- **Known judgment calls:** repair runs inside the daily workflow rather than ad-hoc dispatch (idempotent, ordering after migrations guaranteed); `snapshot-companies` deliberately keeps snapshotting excluded rows (cheap, and history stays intact).
