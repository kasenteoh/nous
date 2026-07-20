"""Owner-approved funding-prominence override.

Product rule (owner call, 2026-07-20): a company with a RECORDED funding round
of at least ``PROMINENCE_OVERRIDE_USD`` ($500,000,000) stays in the shown cohort
regardless of the LLM's is-this-a-startup judgment — the automated
``not_a_startup`` exclusion must NOT fire for it. The owner wants SpaceX-class
private mega-raisers visible.

Provenance: ``blue-origin`` was auto-excluded — correctly, under the old rule —
once its long-fixed website finally became scrapable and the eligibility judge
read it as a mature aerospace company rather than a startup. For a private
company of that funding stature that is the wrong product outcome; this override
is the owner's fix.

Scope: this only suppresses the AUTOMATED ``not_a_startup`` verdict. It never
un-does a manual/ops exclusion (``exclude_company.py`` — ``manual``, ``non_us``,
``parse_artifact``): operator intent always wins.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import FundingRound

# Owner-approved threshold (2026-07-20). A recorded round at or above this keeps
# the company shown regardless of the LLM not_a_startup verdict.
PROMINENCE_OVERRIDE_USD = 500_000_000


async def max_recorded_round_usd(
    session: AsyncSession, company_id: UUID
) -> Decimal | None:
    """Largest ``amount_raised`` across the company's recorded funding rounds.

    One ``SELECT MAX(...)``; ``None`` when the company has no rounds (or only
    amount-less ones). Callers that only need the yes/no override decision use
    :func:`has_prominent_round`; the override guards read the amount here so the
    INFO log can name the actual round in the same single query.
    """
    max_amount: Decimal | None = (
        await session.execute(
            select(func.max(FundingRound.amount_raised)).where(
                FundingRound.company_id == company_id
            )
        )
    ).scalar_one()
    return max_amount


async def has_prominent_round(session: AsyncSession, company_id: UUID) -> bool:
    """True when the company has a recorded round >= ``PROMINENCE_OVERRIDE_USD``.

    The predicate behind the owner's funding-prominence override (see module
    docstring): a prominent recorded raise keeps the company in the shown cohort
    regardless of the LLM's not-a-startup verdict. A company with no rounds
    yields ``False``.
    """
    max_amount = await max_recorded_round_usd(session, company_id)
    return max_amount is not None and max_amount >= PROMINENCE_OVERRIDE_USD
