"""Integration tests for M3 auto-create + fuzzy match helpers.

Covers:
- Exact normalized_name match returns the existing row.
- Trigram match above threshold returns the existing row.
- Trigram match below threshold inserts a new row.
- auto_create_company opportunistic website backfill on match.
- auto_create_company writes discovered_via on insert.
- Re-running auto_create_company is idempotent.

Requires DATABASE_URL pointing at a Postgres with pg_trgm + migration 0003.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.db.upsert import auto_create_company, find_company_by_name
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_company(
    *,
    name: str,
    slug: str,
    normalized_name: str | None = None,
    website: str | None = None,
    discovered_via: str = "vc_portfolio",
) -> Company:
    return Company(
        name=name,
        slug=slug,
        normalized_name=normalized_name if normalized_name is not None else normalize_name(name),
        hq_country="US",
        website=website,
        discovered_via=discovered_via,
    )


# ---------------------------------------------------------------------------
# find_company_by_name
# ---------------------------------------------------------------------------


async def test_find_by_name_exact_match(db: AsyncSession) -> None:
    existing = make_company(
        name="Ricursive Intelligence",
        slug=f"ricursive-intelligence-{os.urandom(3).hex()}",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    found = await find_company_by_name(db, "Ricursive Intelligence")
    assert found is not None
    assert found.id == existing.id


async def test_find_by_name_trigram_above_threshold(db: AsyncSession) -> None:
    """A close-but-not-exact match still finds the row above the 0.85 default."""
    existing = make_company(
        name="Ricursive Intelligence Inc.",
        slug=f"ricursive-intelligence-{os.urandom(3).hex()}",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    # "ricursive intelligence inc" → normalized_name "ricursive intelligence" —
    # exact match strips the suffix, so this hits exact, not trigram. Use a
    # genuinely different but close form instead.
    found = await find_company_by_name(db, "Ricursive Intelligenc")  # missing 'e'
    assert found is not None
    assert found.id == existing.id


async def test_find_by_name_trigram_below_threshold_returns_none(
    db: AsyncSession,
) -> None:
    """A name that's similar but below 0.85 should not match."""
    db.add(
        make_company(
            name="Acme Corp",
            slug=f"acme-corp-{os.urandom(3).hex()}",
        )
    )
    await db.flush()
    await db.commit()

    # "wholly unrelated company" shares no trigrams with "acme corp"
    found = await find_company_by_name(db, "Wholly Unrelated Company")
    assert found is None


async def test_find_by_name_empty_returns_none(db: AsyncSession) -> None:
    found = await find_company_by_name(db, "")
    assert found is None


# ---------------------------------------------------------------------------
# auto_create_company
# ---------------------------------------------------------------------------


async def test_auto_create_inserts_new_row(db: AsyncSession) -> None:
    company, created = await auto_create_company(
        db,
        name="Brand New Co",
        website="https://brandnewco.example/",
        discovered_via="vc_portfolio",
    )
    assert created is True
    assert company.name == "Brand New Co"
    assert company.website == "https://brandnewco.example/"
    assert company.discovered_via == "vc_portfolio"
    assert company.description_short is None  # M2 enrichment will fill later
    # hq_country stays NULL on insert — set by enrich-companies / ccTLD inference,
    # not by the discovery stage (Task 2.3: stop masking non-US companies as US).
    assert company.hq_country is None


async def test_auto_create_returns_existing_on_exact_match(
    db: AsyncSession,
) -> None:
    # Use "Company" not "Co" — "Co" is in slugify._SUFFIX_PATTERN, so
    # normalize_name("Existing Co") == "existing" (not "existingcompany"), which
    # would defeat the exact-match path for this test.
    existing = make_company(
        name="Existing Company",
        slug=f"existing-company-{os.urandom(3).hex()}",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    company, created = await auto_create_company(
        db,
        name="Existing Company",
        website="https://new-website.example/",
        discovered_via="vc_portfolio",
    )
    assert created is False
    assert company.id == existing.id


async def test_auto_create_backfills_website_when_missing(
    db: AsyncSession,
) -> None:
    """An existing row with no website gets the auto-create's URL filled in."""
    existing = make_company(
        name="No Site Company",
        slug=f"no-site-company-{os.urandom(3).hex()}",
        website=None,
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    _, created = await auto_create_company(
        db,
        name="No Site Company",
        website="https://nosite.example/",
        discovered_via="vc_portfolio",
    )
    assert created is False
    await db.commit()

    refetched = await db.get(Company, existing.id)
    assert refetched is not None
    assert refetched.website == "https://nosite.example/"


async def test_auto_create_does_not_overwrite_existing_website(
    db: AsyncSession,
) -> None:
    existing = make_company(
        name="Has Site Company",
        slug=f"has-site-company-{os.urandom(3).hex()}",
        website="https://resolved-by-m2.example/",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    await auto_create_company(
        db,
        name="Has Site Company",
        website="https://vc-claimed-url.example/",
        discovered_via="vc_portfolio",
    )
    await db.commit()

    refetched = await db.get(Company, existing.id)
    assert refetched is not None
    assert refetched.website == "https://resolved-by-m2.example/"


async def test_auto_create_idempotent_on_rerun(db: AsyncSession) -> None:
    """Calling auto_create_company twice for the same name yields one row."""
    company1, created1 = await auto_create_company(
        db,
        name="Idempotent Co",
        website=None,
        discovered_via="news",
    )
    assert created1 is True
    await db.commit()

    company2, created2 = await auto_create_company(
        db,
        name="Idempotent Co",
        website=None,
        discovered_via="news",
    )
    assert created2 is False
    assert company2.id == company1.id


async def test_auto_create_trigram_match_avoids_duplicate(
    db: AsyncSession,
) -> None:
    """A near-miss name (above 0.85 similarity) reuses the existing row."""
    existing = make_company(
        name="Ricursive Intelligence",
        slug=f"ricursive-intelligence-{os.urandom(3).hex()}",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    _, created = await auto_create_company(
        db,
        name="Ricursive Intelligenc",  # typo: missing 'e' — should fuzzy-match
        website=None,
        discovered_via="news",
    )
    assert created is False


# ---------------------------------------------------------------------------
# Cross-source dedup — the canonical OpenAI scenario
# ---------------------------------------------------------------------------


async def test_tc_and_vc_arrivals_merge_to_one_row(
    db: AsyncSession,
) -> None:
    """A TechCrunch arrival and a VC-portfolio arrival for the same company
    collapse to a single row via ``auto_create_company``'s fuzzy matching —
    the legitimate cross-source merge case.

    End state: one row. The TC arrival creates it; the VC arrival (a spacing
    variant) matches via normalize_name and returns the same row, leaving the
    first-seen name and discovery source intact.
    """
    # 1. TechCrunch broad-sweep auto-creates the row.
    tc_company, tc_created = await auto_create_company(
        db,
        name="OpenAI",
        website=None,
        discovered_via="techcrunch",
    )
    assert tc_created is True
    canonical_id = tc_company.id

    # 2. VC portfolio refresh: same company, slight stylization. Matches via
    #    normalize_name → returns the existing row, leaves name alone.
    vc_company, vc_created = await auto_create_company(
        db,
        name="Open AI",  # spacing variant — must collide on the same match key
        website="https://openai.com",
        discovered_via="vc_portfolio",
    )
    assert vc_created is False
    assert vc_company.id == canonical_id

    # First-discovery wins: name and source are untouched, website backfilled.
    refetched = await db.get(Company, canonical_id, populate_existing=True)
    assert refetched is not None
    assert refetched.name == "OpenAI"
    assert refetched.discovered_via == "techcrunch"
    assert refetched.website == "https://openai.com"

    # Exactly one row carries this normalized_name.
    matches = await db.execute(
        select(Company).where(Company.normalized_name == "openai")
    )
    rows = matches.scalars().all()
    assert {r.id for r in rows} == {canonical_id}


async def test_auto_create_upgrades_lowercase_display_name(db: AsyncSession) -> None:
    """An all-lowercase name (e.g. from Greylock) is upgraded when the same
    company arrives properly-cased from another source."""
    existing = make_company(
        name="airbnb",
        slug=f"airbnb-{os.urandom(3).hex()}",
        normalized_name="airbnb",
        discovered_via="vc_portfolio",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    company, created = await auto_create_company(
        db,
        name="Airbnb",
        website=None,
        discovered_via="vc_portfolio",
    )
    assert created is False
    assert company.id == existing.id
    await db.commit()

    refetched = await db.get(Company, existing.id)
    assert refetched is not None
    assert refetched.name == "Airbnb"


async def test_auto_create_does_not_downgrade_cased_name(db: AsyncSession) -> None:
    """A properly-cased existing name is never overwritten by a lowercase one."""
    existing = make_company(
        name="Airbnb",
        slug=f"airbnb-{os.urandom(3).hex()}",
        normalized_name="airbnb",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    await auto_create_company(
        db, name="airbnb", website=None, discovered_via="news"
    )
    await db.commit()

    refetched = await db.get(Company, existing.id)
    assert refetched is not None
    assert refetched.name == "Airbnb"


# ---------------------------------------------------------------------------
# Short-name fuzzy guard
# ---------------------------------------------------------------------------


async def test_short_name_does_not_fuzzy_match(db: AsyncSession) -> None:
    """A normalized name shorter than 6 chars must not fuzzy-match an existing
    company — trigram similarity is unreliable for such short strings (e.g.
    'ai', 'vue', 'x' could match unrelated companies at 0.85).

    The exact-match path is still intact, so exact hits are always found.
    The guard applies only in the trigram branch.

    We use similarity_threshold=0.2 to make this test discriminating:
    "ai" vs "acmeai" would not match even at 0.85 (trigram scores for length-2
    vs length-6 strings are very low), so a high threshold could give a false
    green even if the guard were removed.  At 0.2, trigram("ai", "acmeai")
    would clear the threshold and produce a match — making the guard the ONLY
    thing preventing it.  If the guard is removed, this test fails.
    """
    # Insert a company whose normalized_name is long enough that a short query
    # could score high similarity against it.
    existing = make_company(
        name="Acme AI Corp",
        slug=f"acme-ai-corp-{os.urandom(3).hex()}",
        normalized_name="acmeai",  # 6 chars — this is the *stored* company
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    # "AI" normalizes to "ai" (2 chars) — below the 6-char guard, so the
    # fuzzy branch is skipped and no match is returned.
    # Threshold 0.2: low enough that trigram("ai", "acmeai") would match if
    # the guard were absent — so the guard is what blocks this, not the threshold.
    found = await find_company_by_name(db, "AI", similarity_threshold=0.2)
    assert found is None, (
        "Short normalized name 'ai' should not fuzzy-match an existing company"
    )


async def test_short_name_exact_match_still_works(db: AsyncSession) -> None:
    """The short-name guard must not block exact normalized-name matches —
    it applies only inside the trigram branch."""
    existing = make_company(
        name="X Co",  # normalizes to "x" (1 char after suffix strip)
        slug=f"x-co-{os.urandom(3).hex()}",
        normalized_name="x",
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    # Exact match on "x" must still work even though len("x") < 6.
    found = await find_company_by_name(db, "X Co")
    assert found is not None
    assert found.id == existing.id


async def test_six_char_name_can_fuzzy_match(db: AsyncSession) -> None:
    """A normalized name with exactly 6 chars passes the guard and can
    fuzzy-match a near-identical existing company.

    This is a true boundary test: the stored company normalized_name is exactly
    6 chars ("acmeco"), and the query also normalizes to exactly 6 chars
    ("acmec0" — last char differs).  At threshold=0.2, trigram similarity
    between "acmeco" and "acmec0" is high enough to match.  The guard only
    blocks names with fewer than 6 chars, so len==6 passes through to the
    fuzzy branch.

    Why threshold=0.2: trigram similarity on 6-char strings is lower than on
    long strings, so 0.85 may not match even valid near-duplicates at this
    boundary length.  0.2 is low enough to guarantee the pair matches while
    still discriminating against completely unrelated strings.
    """
    existing = make_company(
        name="Acme Co",
        slug=f"acmeco-{os.urandom(3).hex()}",
        normalized_name="acmeco",  # exactly 6 chars — on the boundary
    )
    db.add(existing)
    await db.flush()
    await db.commit()

    # "Acme C0" (letter O → digit 0) normalizes to "acmec0" — 6 chars, one
    # char different, so trigram similarity ~0.5 at 6 chars.  At threshold=0.2
    # this must match (passes the guard, clears the threshold).
    found = await find_company_by_name(db, "Acme C0", similarity_threshold=0.2)
    assert found is not None, (
        "A 6-char normalized name should pass the guard and be able to fuzzy-match"
    )
    assert found.id == existing.id


# ---------------------------------------------------------------------------
# Norm-empty slug collision (_build_slug deterministic dedup)
# ---------------------------------------------------------------------------


async def test_two_all_symbol_names_yield_distinct_slugs(db: AsyncSession) -> None:
    """Two companies whose names normalize to empty (all-symbol names like
    '!!!' and '---') must get distinct slugs without raising IntegrityError.

    Both names slugify to '' → _build_slug falls back to base='company'.
    Both have website=None → same seed '' → same first candidate 'company-<hash>'.
    Without the counter-loop fix, the second insert would raise IntegrityError.
    With it, the second gets a different suffix via seed+counter.
    """
    company1, created1 = await auto_create_company(
        db,
        name="!!!",
        website=None,
        discovered_via="vc_portfolio",
    )
    assert created1 is True
    await db.commit()

    # Different all-symbol name also normalizes to empty — same slug collision path.
    company2, created2 = await auto_create_company(
        db,
        name="---",
        website=None,
        discovered_via="vc_portfolio",
    )
    assert created2 is True
    await db.commit()

    assert company1.id != company2.id
    assert company1.slug != company2.slug, (
        "Two all-symbol companies must get distinct slugs — "
        "the counter-loop in _build_slug should break the collision"
    )


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
