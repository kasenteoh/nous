"""infer-hq-country repair stage — targeted, idempotent, dispatch-gated.

Detects non-US companies that slipped through the enrich/judge country logic
with hq_country = NULL because the HQ signal was absent from the scraped
homepage/product text. For each shown company that has a website and description
with hq_country IS NULL this stage FETCHES the address-bearing pages the homepage scraper skips
(/about, /contact, /legal, /imprint, /privacy, ...) on the company's OWN
domain, runs a focused country-inference LLM judgment over that text, and —
only on positive, quoted evidence — sets hq_country and soft-excludes non-US
companies as 'non_us' (recording the page URL + quote as the source).
Genuinely unknown, US-plausible companies are left NULL.

Conservative by construction:
  * acts on a country only when the LLM's evidence_quote is an actual substring
    of the fetched text (guards against a hallucinated quote);
  * the prompt judges ONLY the company's own HQ and ignores customer names;
  * US is set only on a concrete quoted US-location; everything ambiguous stays
    NULL.

Resilience + idempotency mirror judge-eligibility: per-company fresh sessions,
db_op_timeout-bounded DB ops, stop-the-loop on LLMRateLimitError, and an
always-written hq_country_checked_at stamp so a second run selects nothing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx
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
from nous.llm.prompts.hq_country import (
    PROMPT_VERSION as HQ_COUNTRY_PROMPT_VERSION,
)
from nous.llm.prompts.hq_country import HqCountryJudgment, build_prompt
from nous.sources.homepage import FetchResult, RobotsBlockedError
from nous.util.ssrf import BlockedAddressError
from nous.util.text import extract_visible_text, truncate_to_chars

logger = logging.getLogger(__name__)

# Per-DB-operation wall-clock bound (mirrors judge-eligibility; see that module
# for the free-tier wedged-connection rationale). The LLM call and the network
# fetches are bounded by their OWN deadlines, not this.
_DB_OP_TIMEOUT_SECONDS: float = 60.0
_CLOSE_TIMEOUT_SECONDS: float = 5.0

# Address-bearing paths the homepage scraper does not guarantee to fetch.
# Fetched text is placed AHEAD of stored homepage text in the prompt window so
# the company's own address (when present) dominates the truncation.
_CANDIDATE_PATHS: tuple[str, ...] = (
    "/about", "/about-us", "/company", "/contact", "/contact-us",
    "/legal", "/imprint", "/impressum", "/privacy", "/privacy-policy",
    "/terms", "/gdpr",
)
# Stop after this many newly-fetched usable pages (bounds politeness + cost).
_MAX_USABLE_PAGES: int = 4
# A fetched page must yield at least this many chars of visible text to be used.
_MIN_PAGE_TEXT_CHARS: int = 60
# Cap the evidence quote stored in exclusion_detail.
_MAX_DETAIL_QUOTE_CHARS: int = 200


class InferHqCountrySummary(BaseModel):
    companies_checked: int = 0
    excluded_non_us: int = 0
    set_us: int = 0
    left_unknown: int = 0
    fetch_failures: int = 0
    llm_failures: int = 0
    skipped_rate_limited: int = 0


class _FetchClient(Protocol):
    """Structural type for the fetch client (HomepageClient in prod; a fake in
    tests). Only ``fetch`` is needed."""

    async def fetch(self, url: str) -> FetchResult: ...


def _normalize_ws(text: str) -> str:
    """Lowercase + collapse whitespace runs to single spaces, stripped."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _normalize_iso2(raw: str | None) -> str | None:
    """Coerce an LLM country value to an uppercase 2-letter code, or None.

    Lenient (per the DeepSeek 'normalize don't reject' lesson): anything that is
    not a clean alpha-2 code becomes None rather than raising.
    """
    if not raw:
        return None
    code = raw.strip().upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else None


def _candidate_urls(website: str) -> list[str]:
    """Build deduped same-origin candidate URLs for the address-bearing paths."""
    try:
        parsed = urlparse(website)
    except Exception:
        return []
    if not parsed.scheme or not parsed.netloc:
        return []
    root = f"{parsed.scheme}://{parsed.netloc}"
    seen: set[str] = set()
    urls: list[str] = []
    for path in _CANDIDATE_PATHS:
        u = root + path
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _evidence_supported(
    quote: str | None, sources: list[tuple[str, str]]
) -> str | None:
    """Return the source URL whose text contains `quote` (normalized), else None.

    The substring check (after whitespace/case normalization) guards against a
    hallucinated quote: act on the LLM's country only when its evidence is
    actually present in text we fetched. Errs toward NOT excluding.
    """
    if not quote:
        return None
    q = _normalize_ws(quote)
    if len(q) < 3:
        return None
    # Match only when the quote is NOT embedded inside a larger alphanumeric
    # token — guards against a short quote coincidentally matching a substring
    # (e.g. "usa" inside "usable"). Real HQ evidence ("Copenhagen", "Berlin,
    # Germany") sits at word boundaries, so recall is unaffected.
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(q)}(?![a-z0-9])")
    for url, text in sources:
        if pattern.search(_normalize_ws(text)):
            return url
    return None


def _apply_judgment(
    company: Company,
    judgment: HqCountryJudgment,
    sources: list[tuple[str, str]],
    *,
    now: datetime,
    summary: InferHqCountrySummary,
    dry_run: bool,
) -> None:
    """Apply the country judgment to `company` in memory. Acts only on a
    validated ISO2 country whose evidence quote is present in the fetched text;
    otherwise leaves hq_country NULL. ALWAYS stamps hq_country_checked_at (unless
    dry_run). Summary counters reflect the INTENDED action even in dry_run."""
    cc = _normalize_iso2(judgment.hq_country)
    source_url = (
        _evidence_supported(judgment.evidence_quote, sources) if cc else None
    )

    if cc and source_url and cc != "US":
        quote = (judgment.evidence_quote or "").strip()[:_MAX_DETAIL_QUOTE_CHARS]
        logger.info(
            "infer-hq-country: exclude %s as non_us (%s) — %s: %r",
            company.name, cc, source_url, quote,
        )
        summary.excluded_non_us += 1
        if not dry_run:
            company.hq_country = cc
            # Provenance stamp: this hq_country came from the hq_country
            # prompt. Stamped only when content is written — a left-unknown
            # attempt is recorded by hq_country_checked_at alone.
            company.hq_country_prompt_version = HQ_COUNTRY_PROMPT_VERSION
            company.exclusion_reason = "non_us"
            company.exclusion_detail = f'HQ {cc} from {source_url}: "{quote}"'
            company.excluded_at = now
    elif cc == "US" and source_url:
        logger.info(
            "infer-hq-country: %s confirmed US — %s", company.name, source_url
        )
        summary.set_us += 1
        if not dry_run:
            company.hq_country = "US"
            company.hq_country_prompt_version = HQ_COUNTRY_PROMPT_VERSION
    else:
        summary.left_unknown += 1

    if not dry_run:
        company.hq_country_checked_at = now


async def _safe_close(session: AsyncSession) -> None:
    """Best-effort, self-bounded session close (mirrors judge-eligibility)."""
    try:
        async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
            await session.close()
    except Exception:  # noqa: BLE001 — best-effort cleanup of a wedged connection
        logger.debug("Bounded session close failed/timed out; abandoning session.")


async def _gather_text(
    client: _FetchClient,
    company: Company,
    stored_pages: list[RawPage],
    summary: InferHqCountrySummary,
) -> list[tuple[str, str]]:
    """Fetch address-bearing pages on the company's own domain, then append
    stored homepage text. Returns ordered (url, visible_text) sources — fetched
    pages first so they dominate the truncated prompt window."""
    sources: list[tuple[str, str]] = []
    fetched = 0
    if company.website:
        for url in _candidate_urls(company.website):
            if fetched >= _MAX_USABLE_PAGES:
                break
            try:
                result = await client.fetch(url)
            except (
                RobotsBlockedError,
                httpx.HTTPStatusError,
                httpx.RequestError,
                BlockedAddressError,
            ):
                continue
            text = extract_visible_text(result.content)
            if len(text) >= _MIN_PAGE_TEXT_CHARS:
                sources.append((result.url, text))
                fetched += 1
    if fetched == 0:
        summary.fetch_failures += 1
    for page in stored_pages:
        text = extract_visible_text(page.content)
        if text:
            sources.append((page.url, text))
    return sources


async def _process_one_company(
    session: AsyncSession,
    client: _FetchClient,
    company_id: UUID,
    summary: InferHqCountrySummary,
    *,
    dry_run: bool,
    db_op_timeout: float,
) -> None:
    async with asyncio.timeout(db_op_timeout):
        company = await session.get(Company, company_id)
        if company is None:
            # Selected a moment ago, gone now — a concurrent dedup merge.
            summary.llm_failures += 1
            return
        stored_pages = list(
            (
                await session.execute(
                    select(RawPage)
                    .where(RawPage.company_id == company.id)
                    .order_by(RawPage.url.asc())
                )
            ).scalars().all()
        )

    # Network fetches: bounded by the client's own timeouts/retries.
    sources = await _gather_text(client, company, stored_pages, summary)
    cleaned = truncate_to_chars(
        "\n\n".join(t for _, t in sources), MAX_PROMPT_INPUT_CHARS
    )
    prompt = build_prompt(
        company_name=company.name,
        description=company.description_short or "",
        cleaned_text=cleaned or "(no text on record)",
    )
    # Bounded by the LLM client's own overall deadline, NOT db_op_timeout.
    judgment: HqCountryJudgment = await complete_json(prompt, HqCountryJudgment)

    now = datetime.now(tz=UTC)
    _apply_judgment(
        company, judgment, sources, now=now, summary=summary, dry_run=dry_run
    )
    if not dry_run:
        session.add(company)
        async with asyncio.timeout(db_op_timeout):
            await session.commit()
    summary.companies_checked += 1


async def run_infer_hq_country(
    session_factory: async_sessionmaker[AsyncSession],
    client: _FetchClient,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    db_op_timeout: float = _DB_OP_TIMEOUT_SECONDS,
) -> InferHqCountrySummary:
    summary = InferHqCountrySummary()

    # Select the work-list (ids only) in its own short session, then close it.
    async with session_factory() as session:
        stmt = (
            select(Company.id, Company.name)
            .where(Company.exclusion_reason.is_(None))
            .where(Company.hq_country.is_(None))
            .where(Company.website.is_not(None))
            .where(Company.description_short.is_not(None))
            .where(Company.hq_country_checked_at.is_(None))
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
            await _process_one_company(
                session, client, company_id, summary,
                dry_run=dry_run, db_op_timeout=db_op_timeout,
            )
        except TimeoutError:
            logger.warning(
                "infer-hq-country DB op for %s exceeded %.0fs (wedged free-tier "
                "connection?) — skipping; continuing on a fresh session.",
                company_name, db_op_timeout,
            )
            summary.llm_failures += 1
        except LLMRateLimitError as exc:
            logger.warning(
                "LLM rate limit while inferring country for %s — stopping loop. "
                "Raw: %s", company_name, exc,
            )
            summary.skipped_rate_limited += 1
            rate_limited = True
        except (LLMParseError, LLMError) as exc:
            logger.warning(
                "LLM error inferring country for %s: %s", company_name, exc
            )
            summary.llm_failures += 1
        except (StaleDataError, IntegrityError):
            logger.warning(
                "Company %s disappeared mid-infer (concurrent merge) — skipping.",
                company_id,
            )
            summary.llm_failures += 1
        finally:
            await _safe_close(session)
        if rate_limited:
            break

    return summary
