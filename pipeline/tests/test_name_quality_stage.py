"""DB-gated integration tests for the name-quality stage.

Requires DATABASE_URL pointing at a Postgres with the schema at head (same
gating as the other stage suites). Exercises ``run_name_quality`` over real
rows: a casing upgrade is applied from the prepended homepage title line, a
different-word title is ignored (it does not normalize to the same company), an
already-good name is a no-op, --dry-run writes nothing, and a second run is
idempotent. The pure decision helpers are unit-tested (no DB) in
test_name_quality.py.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.pipeline.name_quality import run_name_quality
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(name: str) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{normalize_name(name) or 'company'}-{suffix}",
        normalized_name=normalize_name(name),
        website="https://example-brand.com",
        hq_country="US",
    )


def _homepage(company_id: object, title_line: str) -> RawPage:
    """A RawPage whose content begins with the prepended title line, as
    extract_visible_text would store it (title first, then body)."""
    return RawPage(
        company_id=company_id,  # type: ignore[arg-type]
        url="https://example-brand.com/",
        content=f"{title_line}\nBody copy about the product.",
    )


async def test_casing_upgrade_applied(db: AsyncSession) -> None:
    """A homepage title of "DocuSign" upgrades an all-lowercase "docusign"."""
    co = _make_company("docusign")
    db.add(co)
    await db.flush()
    db.add(_homepage(co.id, "DocuSign | The #1 way to send and sign"))
    await db.commit()
    co_id = co.id
    original_slug, original_norm = co.slug, co.normalized_name

    summary = await run_name_quality(db)
    assert summary.names_upgraded == 1

    refetched = await db.get(Company, co_id)
    assert refetched is not None
    assert refetched.name == "DocuSign"
    # slug + normalized_name are untouched (both already lowercase).
    assert refetched.slug == original_slug
    assert refetched.normalized_name == original_norm


async def test_different_word_not_applied(db: AsyncSession) -> None:
    """A title naming a DIFFERENT brand (Globex) never renames Acme — it does
    not normalize to the same company, so it is skipped."""
    co = _make_company("Acme")
    db.add(co)
    await db.flush()
    db.add(_homepage(co.id, "Globex — We build the future"))
    await db.commit()
    co_id = co.id

    summary = await run_name_quality(db)
    assert summary.names_upgraded == 0

    refetched = await db.get(Company, co_id)
    assert refetched is not None
    assert refetched.name == "Acme"


async def test_already_good_casing_is_noop(db: AsyncSession) -> None:
    """A correctly-cased name with a matching title is left unchanged."""
    co = _make_company("DocuSign")
    db.add(co)
    await db.flush()
    db.add(_homepage(co.id, "DocuSign | eSignature and Agreements"))
    await db.commit()
    co_id = co.id

    summary = await run_name_quality(db)
    assert summary.names_upgraded == 0

    refetched = await db.get(Company, co_id)
    assert refetched is not None
    assert refetched.name == "DocuSign"


async def test_dry_run_writes_nothing(db: AsyncSession) -> None:
    """--dry-run reports the upgrade but does not mutate the row."""
    co = _make_company("docusign")
    db.add(co)
    await db.flush()
    db.add(_homepage(co.id, "DocuSign | eSignature"))
    await db.commit()
    co_id = co.id

    summary = await run_name_quality(db, dry_run=True)
    assert summary.names_upgraded == 1  # it WOULD upgrade

    refetched = await db.get(Company, co_id)
    assert refetched is not None
    assert refetched.name == "docusign"  # but did not


async def test_second_run_is_idempotent(db: AsyncSession) -> None:
    co = _make_company("docusign")
    db.add(co)
    await db.flush()
    db.add(_homepage(co.id, "DocuSign | eSignature"))
    await db.commit()

    first = await run_name_quality(db)
    assert first.names_upgraded == 1

    second = await run_name_quality(db)
    assert second.names_upgraded == 0
