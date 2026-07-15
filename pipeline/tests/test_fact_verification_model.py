"""DB-gated tests for the fact_verifications table (migration 0043 + model).

Requires DATABASE_URL (Postgres with `alembic upgrade head` applied); skipped
otherwise. Exercises the round-trip, the UNIQUE(company_id, fact_kind, fact_ref)
upsert key, the fact_kind / verdict CHECK vocabularies, and the CASCADE.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _company(name: str = "Acme") -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{name.lower()}-{suffix}",
        normalized_name=f"{name.lower()}{suffix}",
    )


async def test_round_trip(db: AsyncSession) -> None:
    c = _company()
    db.add(c)
    await db.flush()
    db.add(
        FactVerification(
            company_id=c.id,
            fact_kind="total_raised",
            fact_ref="",
            source_url="https://techcrunch.com/acme",
            claim="Acme has raised a total of $12M.",
            verdict="supported",
            supporting_quote="raised $12 million",
            prompt_version="2026-07-14.1",
        )
    )
    await db.flush()
    row = (
        await db.execute(
            select(FactVerification).where(FactVerification.company_id == c.id)
        )
    ).scalar_one()
    assert row.verdict == "supported"
    assert row.supporting_quote == "raised $12 million"
    assert row.fact_ref == ""


async def test_unique_company_fact(db: AsyncSession) -> None:
    c = _company()
    db.add(c)
    await db.flush()
    for _ in range(2):
        db.add(
            FactVerification(
                company_id=c.id,
                fact_kind="funding_round",
                fact_ref="round-123",
                source_url="https://techcrunch.com/x",
                claim="Acme raised $5M in its Seed round.",
                verdict="uncertain",
                prompt_version="2026-07-14.1",
            )
        )
    with pytest.raises(IntegrityError):
        await db.flush()


@pytest.mark.parametrize(
    ("field", "value"),
    [("fact_kind", "bogus_kind"), ("verdict", "maybe")],
)
async def test_check_constraints_reject_bad_vocab(
    db: AsyncSession, field: str, value: str
) -> None:
    c = _company()
    db.add(c)
    await db.flush()
    kwargs = dict(
        company_id=c.id,
        fact_kind="status",
        fact_ref="",
        source_url="https://techcrunch.com/x",
        claim="Acme has been acquired.",
        verdict="supported",
        supporting_quote="Acme was acquired",
        prompt_version="2026-07-14.1",
    )
    kwargs[field] = value
    db.add(FactVerification(**kwargs))  # type: ignore[arg-type]
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_cascade_delete(db: AsyncSession) -> None:
    c = _company()
    db.add(c)
    await db.flush()
    db.add(
        FactVerification(
            company_id=c.id,
            fact_kind="status",
            fact_ref="",
            source_url="https://techcrunch.com/x",
            claim="Acme has shut down.",
            verdict="unsupported",
            prompt_version="2026-07-14.1",
        )
    )
    await db.flush()
    await db.delete(c)
    await db.flush()
    remaining = (
        await db.execute(
            select(FactVerification).where(FactVerification.company_id == c.id)
        )
    ).scalars().all()
    assert remaining == []
