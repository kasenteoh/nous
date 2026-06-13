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
