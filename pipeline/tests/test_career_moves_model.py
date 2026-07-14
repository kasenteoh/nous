"""DB-gated schema-behavior tests for the career_moves table (migration 0040).

Exercises the schema decisions that matter and can't be seen from the model
source: the unique idempotency key, and the deliberate CASCADE (on the owning
company) vs SET NULL (on the resolved prior-company link) delete behaviors.
Skipped without DATABASE_URL; runs in CI's Postgres service.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import CareerMove, Company
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

_V = "2026-07-13.1"


def _company(name: str) -> Company:
    suffix = os.urandom(4).hex()
    return Company(
        name=name,
        slug=f"{normalize_name(name) or 'co'}-{suffix}",
        normalized_name=normalize_name(name),
    )


def _move(company_id: object, prior_name: str, **kw: object) -> CareerMove:
    return CareerMove(
        company_id=company_id,
        person_name="Jane Doe",
        person_normalized_name=normalize_name("Jane Doe"),
        prior_company_name=prior_name,
        extraction_prompt_version=_V,
        **kw,  # type: ignore[arg-type]
    )


async def test_roundtrip_and_defaults(db: AsyncSession) -> None:
    co = _company("Acme")
    db.add(co)
    await db.flush()
    db.add(_move(co.id, "Stripe", prior_role="Engineer", start_year=2015))
    await db.flush()

    row = (
        await db.execute(select(CareerMove).where(CareerMove.company_id == co.id))
    ).scalar_one()
    assert row.id is not None  # server-default gen_random_uuid()
    assert row.created_at is not None and row.updated_at is not None
    assert row.prior_company_name == "Stripe"
    assert row.prior_company_id is None  # unresolved by default


async def test_unique_constraint_blocks_duplicate_edge(db: AsyncSession) -> None:
    co = _company("Acme")
    db.add(co)
    await db.flush()
    db.add(_move(co.id, "Stripe"))
    await db.flush()
    # Same (company, normalized person, prior company) → the idempotency key rejects it.
    db.add(_move(co.id, "Stripe"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_company_delete_cascades(db: AsyncSession) -> None:
    co = _company("Acme")
    db.add(co)
    await db.flush()
    db.add(_move(co.id, "Stripe"))
    await db.flush()

    # Core DELETE (synchronize_session off) so the DB-level ON DELETE fires and
    # the session's identity map isn't consulted / expired.
    await db.execute(
        delete(Company).where(Company.id == co.id),
        execution_options={"synchronize_session": False},
    )
    remaining = (
        await db.execute(
            select(CareerMove.id).where(CareerMove.company_id == co.id)
        )
    ).all()
    assert remaining == []  # ON DELETE CASCADE removed the row


async def test_prior_company_delete_sets_null_not_delete(db: AsyncSession) -> None:
    current = _company("Acme")
    prior = _company("Oracle")
    db.add_all([current, prior])
    await db.flush()
    db.add(_move(current.id, "Oracle", prior_company_id=prior.id))
    await db.flush()

    # Core DELETE (synchronize_session off) so the DB-level ON DELETE SET NULL fires.
    await db.execute(
        delete(Company).where(Company.id == prior.id),
        execution_options={"synchronize_session": False},
    )
    # Read the columns directly (not the ORM entity) to observe fresh DB state.
    prior_id, prior_name = (
        await db.execute(
            select(CareerMove.prior_company_id, CareerMove.prior_company_name).where(
                CareerMove.company_id == current.id
            )
        )
    ).one()
    # The biographical fact survives; only the internal link is nulled.
    assert prior_id is None
    assert prior_name == "Oracle"
