"""judge-eligibility pipeline stage (one-time backfill, safe to keep running).

Runs the is-this-a-startup judgment over companies that were enriched BEFORE
enrich-companies started making it (description present, eligibility never
checked). Reads stored raw_pages text; never re-writes descriptions.

Commit cadence: one commit per company. Rate-limit handling: stop the loop on
LLMRateLimitError (same pattern as enrich-companies). Selection is stamped via
eligibility_checked_at, so bounded daily runs drain the backlog and steady
state selects nothing (new enrichments stamp themselves).

Connection resilience: each company is processed in its OWN short-lived session
drawn from a session factory, so every company starts on a freshly pre-pinged
connection, and the per-company DB operations are bounded by ``db_op_timeout``.
A high-``limit`` drain on 2026-06-13 hung for 28 minutes on a single wedged
free-tier connection: the pooler dropped the socket during an LLM call and the
next statement stalled in TCP retransmit, which server-side statement_timeout
cannot catch (the query never reaches the server). Bounding each DB op client-
side caps that blast radius to one company — the stage logs it, counts it as a
failure, and continues on a fresh session.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
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
from nous.pipeline.enrich_companies import _infer_country_from_url
from nous.util.text import extract_visible_text, truncate_to_chars

logger = logging.getLogger(__name__)

# Per-DB-operation wall-clock bound. The free-tier pooler occasionally drops a
# connection mid-session; the next statement then hangs in TCP retransmit, which
# server-side statement_timeout cannot catch because the query never reaches the
# server. 60s matches that server-side statement_timeout and is generous for any
# healthy read/commit. The LLM call is bounded SEPARATELY by its own deadline
# (llm/client._CALL_DEADLINE_SECONDS) and is intentionally not wrapped here, so a
# legitimately slow completion is never mistaken for a wedge.
_DB_OP_TIMEOUT_SECONDS: float = 60.0

# A wedged connection can hang even the implicit ROLLBACK that close() issues, so
# the best-effort close is itself bounded by this before the session is abandoned.
_CLOSE_TIMEOUT_SECONDS: float = 5.0


class JudgeEligibilitySummary(BaseModel):
    companies_judged: int = 0
    companies_excluded: int = 0
    llm_failures: int = 0
    skipped_rate_limited: int = 0


async def _safe_close(session: AsyncSession) -> None:
    """Best-effort, self-bounded session close.

    After a connection wedges, even the ROLLBACK that ``close()`` issues can hang
    on the dead socket — so cap the close too and, on failure, abandon the
    session object. The dead connection is reaped by the pool/GC and the process
    exits at end of stage; a leaked socket beats a hung stage.
    """
    try:
        async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
            await session.close()
    except Exception:  # noqa: BLE001 — best-effort cleanup of a wedged connection
        logger.debug("Bounded session close failed/timed out; abandoning session.")


async def _judge_one_company(
    session: AsyncSession,
    company_id: UUID,
    summary: JudgeEligibilitySummary,
    *,
    db_op_timeout: float,
) -> None:
    """Judge ONE company inside its own (fresh) session.

    The DB read and the commit are each bounded by ``db_op_timeout`` so a wedged
    connection aborts this one company instead of the whole stage. On success it
    stamps ``summary`` and returns; on failure it raises (a wedged DB op as
    ``TimeoutError``; LLM errors; a concurrent delete as StaleData/Integrity),
    which the caller maps to summary counters before moving on.
    """
    async with asyncio.timeout(db_op_timeout):
        company = await session.get(Company, company_id)
        if company is None:
            # Selected a moment ago, gone now — a concurrent dedup merge.
            summary.llm_failures += 1
            return
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

    # Bounded by the LLM client's own overall deadline, NOT db_op_timeout.
    judgment: EligibilityJudgment = await complete_json(prompt, EligibilityJudgment)

    now = datetime.now(tz=UTC)
    company.eligibility_checked_at = now
    if judgment.founded_year and not company.year_incorporated:
        company.year_incorporated = judgment.founded_year
    # Country resolution — mirrors the enrich-companies three-tier logic:
    #   1. LLM explicit statement (highest confidence).
    #   2. ccTLD of the company website (deterministic, no cost).
    #   3. US state/city already set → infer US.
    # Only set US when there is positive evidence; leave NULL otherwise.
    llm_country = (judgment.hq_country or "").strip().upper() or None
    if llm_country:
        company.hq_country = llm_country
    elif not company.hq_country:
        cctld_country = _infer_country_from_url(company.website)
        if cctld_country:
            company.hq_country = cctld_country
        elif company.hq_state or company.hq_city:
            company.hq_country = "US"

    if judgment.is_startup is False:
        company.exclusion_reason = "not_a_startup"
        company.exclusion_detail = judgment.not_startup_reason
        company.excluded_at = now
        summary.companies_excluded += 1
    elif company.hq_country is not None and company.hq_country != "US":
        company.exclusion_reason = "non_us"
        company.exclusion_detail = (
            f"HQ country inferred as {company.hq_country}"
            + (f" (LLM: {llm_country})" if llm_country else " (ccTLD)")
        )
        company.excluded_at = now
        summary.companies_excluded += 1

    session.add(company)
    async with asyncio.timeout(db_op_timeout):
        await session.commit()
    summary.companies_judged += 1


async def run_judge_eligibility(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: int | None = None,
    db_op_timeout: float = _DB_OP_TIMEOUT_SECONDS,
) -> JudgeEligibilitySummary:
    summary = JudgeEligibilitySummary()

    # Select the work-list (ids only) in its own short session, then close it.
    async with session_factory() as session:
        stmt = (
            select(Company.id, Company.name)
            .where(Company.description_short.is_not(None))
            .where(Company.eligibility_checked_at.is_(None))
            .where(Company.exclusion_reason.is_(None))
            .order_by(Company.name.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        selected = (await session.execute(stmt)).all()

    for company_id, company_name in selected:
        # Fresh session per company: a freshly pre-pinged connection, and a
        # wedge on one cannot poison the next.
        session = session_factory()
        rate_limited = False
        try:
            await _judge_one_company(
                session, company_id, summary, db_op_timeout=db_op_timeout
            )
        except TimeoutError:
            logger.warning(
                "Judge DB op for %s exceeded %.0fs (wedged free-tier "
                "connection?) — skipping this company; the stage continues on a "
                "fresh session.",
                company_name,
                db_op_timeout,
            )
            summary.llm_failures += 1
        except LLMRateLimitError as exc:
            logger.warning(
                "LLM rate limit hit while judging %s — stopping loop. Raw: %s",
                company_name,
                exc,
            )
            summary.skipped_rate_limited += 1
            rate_limited = True
        except (LLMParseError, LLMError) as exc:
            logger.warning("LLM error judging %s: %s", company_name, exc)
            summary.llm_failures += 1
        except (StaleDataError, IntegrityError):
            logger.warning(
                "Company %s disappeared mid-judge (likely a concurrent merge)"
                " — skipping.",
                company_id,
            )
            summary.llm_failures += 1
        finally:
            await _safe_close(session)
        if rate_limited:
            break

    return summary
