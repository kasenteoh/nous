r"""verify-sources — DeepSeek source-verification, husk-style (measure-first).

Two read-first instruments toward the "✓ Verified against source" enhancement
(spec ``docs/superpowers/specs/2026-07-14-provenance-ui-design.md`` → "Optional
enhancement: source-verification"):

**verify-sources-probe** ($0, read-only, no LLM, no writes) — a prevalence
census over the *shown* cohort. For every rendered fact that carries a cited
source_url (total raised → ``total_raised_source_url``; a non-active status →
``status_source_url``; each funding round → ``primary_news_url``) it buckets the
source by verifiability:

  - **stored** — nous already has the source TEXT (``news_articles.raw_content``
    keyed on url, or ``raw_pages.content`` keyed on (company, url)) → verifiable
    at $ = LLM-only, no re-fetch.
  - **refetch** — an http(s) source with no stored text → verifiable after a
    polite re-fetch (the apply stage's job; scraping etiquette applies).
  - **unreachable** — a bare ``news.google.com`` redirect with no stored text →
    an opaque consent interstitial, not directly fetchable → cannot verify.
  - **unparseable** — no host (relative / malformed) → cannot verify.

This sizes the addressable set (stored + refetch) and the scraping load BEFORE
any spend, mirroring ``career-history-probe`` (the $0 gate before talent-flow).

**verify-sources --dry-run** (paid, bounded) — verifies a prominence-ordered
slice of **stored-text** facts against DeepSeek and prints the
supported/unsupported/uncertain rate, a **fabrication proxy** (a ``supported``
verdict whose quote is NOT a real substring of the source — the moat-critical
"any false-support" check), and the $/fact. It writes NOTHING — the husk gate
before the ``fact_verifications`` schema (migration 0043) and the persisting
apply path are built. The **refetch** bucket is deliberately out of scope for the
dry run: it measures the cheapest, no-request path first; re-fetching (with
robots.txt + contact-email UA + 1 req/sec) lands with the apply stage.

Only ``supported`` (with a grounded quote) may ever earn the public ✓ —
``uncertain``/``unsupported`` are never marked verified (empty-not-fabricate).
An ``unsupported`` verdict is a valuable INTERNAL data-quality signal (surfaced
here / in logs, never as a scary public badge).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    exists,
    func,
    nulls_last,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from nous.db.models import Company, FundingRound, NewsArticle, RawPage
from nous.llm.client import LLMError, complete_json
from nous.llm.prompts.source_verification import (
    PROMPT_VERSION,
    SourceVerification,
    build_prompt,
    quote_is_grounded,
)
from nous.observability import write_step_summary
from nous.util.text import truncate_to_chars
from nous.util.url import hostname

logger = logging.getLogger(__name__)

# The Google News RSS host — its ``/rss/articles/…`` links are opaque redirects
# that resolve to a consent interstitial, so a source_url on this host with no
# stored text cannot be verified (mirrors sources.news._GOOGLE_NEWS_HOST).
_GOOGLE_NEWS_HOST = "news.google.com"

# Minimum stored-text length to count a source as verifiable — below this the
# "text" is a scrape stub / boilerplate with nothing to check against.
_MIN_SOURCE_CHARS = 200

# Cap the source text fed to the model (≈8k tokens), like every other prompt.
MAX_PROMPT_INPUT_CHARS = 32_000

# The three rendered fact kinds that carry a per-fact source_url.
FACT_KINDS = ("total_raised", "status", "funding_round")

# Human-readable claim fragments for a non-active lifecycle status.
_STATUS_CLAIM = {
    "acquired": "has been acquired",
    "shut_down": "has shut down",
    "ipo": "has gone public (IPO)",
}

Bucket = str  # "stored" | "refetch" | "unreachable" | "unparseable"


# ── shown cohort ──────────────────────────────────────────────────────────────


def _shown() -> ColumnElement[bool]:
    """The public-surface predicate (mirrors compute_completeness / the web bar)."""
    return and_(
        Company.exclusion_reason.is_(None),
        or_(
            Company.description_short.is_not(None),
            Company.funding_round_count > 0,
        ),
    )


def classify_source(url: str | None, *, has_stored_text: bool) -> Bucket:
    """Bucket a fact's source_url by how (if at all) it can be verified.

    Pure + host-only, so it is unit-testable without a DB. ``has_stored_text``
    is decided by the caller (an EXISTS over news_articles / raw_pages).
    """
    if has_stored_text:
        return "stored"
    host = hostname(url) if url else ""
    if not host:
        return "unparseable"
    if host == _GOOGLE_NEWS_HOST:
        return "unreachable"
    return "refetch"


# ── claim construction ────────────────────────────────────────────────────────


def _format_usd(amount: Decimal | None) -> str:
    """A compact human dollar figure for a claim ("$12.4B", "$110M", "$500K")."""
    if amount is None:
        return "an undisclosed amount"
    value = float(amount)
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        millions = value / 1e6
        return f"${millions:.0f}M" if millions >= 100 else f"${millions:.1f}M"
    if value >= 1e3:
        return f"${value / 1e3:.0f}K"
    return f"${value:.0f}"


def total_raised_claim(name: str, amount: Decimal | None, as_of: date | None) -> str:
    """The claim nous renders for a company's stated cumulative total raised."""
    claim = f"{name} has raised a total of {_format_usd(amount)}"
    if as_of is not None:
        claim += f" (as of {as_of.isoformat()})"
    return claim + "."


def status_claim(name: str, status: str) -> str:
    """The claim for a non-active lifecycle status."""
    phrase = _STATUS_CLAIM.get(status, f"is {status}")
    return f"{name} {phrase}."


def funding_round_claim(
    name: str,
    amount: Decimal | None,
    round_type: str | None,
    valuation: Decimal | None,
    announced_date: date | None,
) -> str:
    """The claim for a single funding round."""
    claim = f"{name} raised {_format_usd(amount)}"
    if round_type:
        claim += f" in its {round_type} round"
    if valuation is not None:
        claim += f" at a {_format_usd(valuation)} post-money valuation"
    if announced_date is not None:
        claim += f", announced {announced_date.isoformat()}"
    return claim + "."


# ── prevalence probe ($0) ─────────────────────────────────────────────────────


class VerifySourcesProbeSummary(BaseModel):
    """Prevalence of sourced, verifiable facts over the shown cohort."""

    total_facts: int
    facts_by_kind: dict[str, int]
    stored: int
    refetch: int
    unreachable: int
    unparseable: int
    stored_pct: float
    addressable: int  # stored + refetch
    addressable_pct: float
    buckets_by_kind: dict[str, dict[str, int]]


def _has_stored_text(
    source_col: InstrumentedAttribute[str | None],
    company_id_col: InstrumentedAttribute[UUID],
) -> ColumnElement[bool]:
    """An EXISTS: stored source text for ``source_col`` (news body or scraped page).

    A funding article is keyed by ``news_articles.url``; a company-site source is
    keyed by ``raw_pages(company_id, url)``. Either with ≥ ``_MIN_SOURCE_CHARS``
    of text counts as stored (verifiable without a re-fetch).
    """
    news = exists().where(
        and_(
            NewsArticle.url == source_col,
            func.length(NewsArticle.raw_content) >= _MIN_SOURCE_CHARS,
        )
    )
    page = exists().where(
        and_(
            RawPage.company_id == company_id_col,
            RawPage.url == source_col,
            func.length(RawPage.content) >= _MIN_SOURCE_CHARS,
        )
    )
    return or_(news, page)


async def _bucket_facts(
    session: AsyncSession, base_stmt: Select[tuple[str | None, bool]]
) -> dict[Bucket, int]:
    """Run ``base_stmt`` (selecting url + has_stored_text) and tally buckets."""
    rows = (await session.execute(base_stmt)).all()
    counts: dict[Bucket, int] = {
        "stored": 0,
        "refetch": 0,
        "unreachable": 0,
        "unparseable": 0,
    }
    for url, has_text in rows:
        counts[classify_source(url, has_stored_text=bool(has_text))] += 1
    return counts


async def run_verify_sources_probe(
    session: AsyncSession,
) -> VerifySourcesProbeSummary:
    """Census the shown cohort's sourced facts by verifiability. Read-only, $0."""
    total_raised_stmt = select(
        Company.total_raised_source_url,
        _has_stored_text(Company.total_raised_source_url, Company.id),
    ).where(
        _shown(),
        Company.total_raised_usd.is_not(None),
        Company.total_raised_source_url.is_not(None),
    )

    status_stmt = select(
        Company.status_source_url,
        _has_stored_text(Company.status_source_url, Company.id),
    ).where(
        _shown(),
        Company.status != "active",
        Company.status_source_url.is_not(None),
    )

    round_stmt = (
        select(
            FundingRound.primary_news_url,
            _has_stored_text(FundingRound.primary_news_url, FundingRound.company_id),
        )
        .select_from(FundingRound)
        .join(Company, Company.id == FundingRound.company_id)
        .where(_shown(), FundingRound.primary_news_url.is_not(None))
    )

    by_kind = {
        "total_raised": await _bucket_facts(session, total_raised_stmt),
        "status": await _bucket_facts(session, status_stmt),
        "funding_round": await _bucket_facts(session, round_stmt),
    }

    stored = sum(b["stored"] for b in by_kind.values())
    refetch = sum(b["refetch"] for b in by_kind.values())
    unreachable = sum(b["unreachable"] for b in by_kind.values())
    unparseable = sum(b["unparseable"] for b in by_kind.values())
    total = stored + refetch + unreachable + unparseable
    addressable = stored + refetch

    return VerifySourcesProbeSummary(
        total_facts=total,
        facts_by_kind={k: sum(v.values()) for k, v in by_kind.items()},
        stored=stored,
        refetch=refetch,
        unreachable=unreachable,
        unparseable=unparseable,
        stored_pct=round(stored / total * 100, 1) if total else 0.0,
        addressable=addressable,
        addressable_pct=round(addressable / total * 100, 1) if total else 0.0,
        buckets_by_kind=by_kind,
    )


def emit_verify_sources_probe_summary(summary: VerifySourcesProbeSummary) -> None:
    """Append the prevalence report to the GitHub Actions step summary."""
    lines: list[str] = []
    lines.append("## Source-verification prevalence probe")
    lines.append("")
    lines.append(
        f"**{summary.addressable} of {summary.total_facts} sourced facts "
        f"({summary.addressable_pct:.1f}%) are verifiable** "
        f"({summary.stored} from stored text, {summary.refetch} via re-fetch); "
        f"{summary.unreachable} are unreachable Google News redirects."
    )
    lines.append("")
    lines.append(
        "Cohort: shown companies (`exclusion_reason IS NULL` and described or "
        "funded). One fact per total-raised, per non-active status, per funding "
        "round that carries a source_url. Read-only, no LLM, no writes."
    )
    lines.append("")
    lines.append("### Buckets by fact kind")
    lines.append("| Fact kind | Facts | stored | refetch | unreachable | unparseable |")
    lines.append("|---|--:|--:|--:|--:|--:|")
    for kind in FACT_KINDS:
        b = summary.buckets_by_kind.get(kind, {})
        lines.append(
            f"| {kind} | {summary.facts_by_kind.get(kind, 0)} | {b.get('stored', 0)} "
            f"| {b.get('refetch', 0)} | {b.get('unreachable', 0)} "
            f"| {b.get('unparseable', 0)} |"
        )
    lines.append(
        f"| **total** | **{summary.total_facts}** | **{summary.stored}** "
        f"| **{summary.refetch}** | **{summary.unreachable}** "
        f"| **{summary.unparseable}** |"
    )
    lines.append("")
    lines.append("### Read")
    lines.append(
        "- **stored** — verifiable now at $ = LLM-only (the dry run measures these)."
    )
    lines.append(
        "- **refetch** — verifiable after a polite re-fetch (apply stage; sizes "
        "the scraping load)."
    )
    lines.append(
        "- **unreachable / unparseable** — cannot verify; the ✓ simply never shows "
        "for these facts (empty-not-fabricate)."
    )
    write_step_summary("\n".join(lines))


# ── bounded LLM dry run ───────────────────────────────────────────────────────


@dataclass
class _Fact:
    """One verifiable (stored-text) fact selected for the dry run."""

    company_id: UUID
    company_slug: str
    company_name: str
    fact_kind: str
    fact_label: str
    source_url: str
    claim: str
    prominence: float
    source_text: str = ""


class FactVerdict(BaseModel):
    """The per-fact result carried into the yield table."""

    slug: str
    name: str
    fact_kind: str
    fact_label: str
    claim: str
    source_host: str
    verdict: str  # effective verdict (post grounding-check)
    grounded: bool  # a 'supported' whose quote is a real substring of the source
    quote: str | None = None
    error: str | None = None


class VerifySourcesSummary(BaseModel):
    """Result of one verify-sources run."""

    dry_run: bool
    prompt_version: str
    facts_seen: int = 0
    supported: int = 0
    unsupported: int = 0
    uncertain: int = 0
    # A 'supported' verdict whose quote is NOT a verbatim substring of the
    # source — the moat-critical false-support / fabrication proxy. Downgraded to
    # uncertain (never counted as supported), surfaced here as a red flag.
    fabrication_flags: int = 0
    errors: int = 0
    results: list[FactVerdict] = []
    rows_written: int = 0  # reserved for the apply path


async def _load_source_text(
    session: AsyncSession, company_id: UUID, source_url: str
) -> str | None:
    """Stored source text for a fact — the news body, else the scraped page."""
    news = (
        await session.execute(
            select(NewsArticle.raw_content).where(NewsArticle.url == source_url).limit(1)
        )
    ).scalar_one_or_none()
    if news and len(news) >= _MIN_SOURCE_CHARS:
        return news
    page = (
        await session.execute(
            select(RawPage.content)
            .where(RawPage.company_id == company_id, RawPage.url == source_url)
            .limit(1)
        )
    ).scalar_one_or_none()
    if page and len(page) >= _MIN_SOURCE_CHARS:
        return page
    return None


async def _collect_stored_text_facts(
    session: AsyncSession, *, limit: int
) -> list[_Fact]:
    """Prominence-ordered stored-text facts across the three kinds, capped at limit.

    Each kind is queried has-stored-text-only and prominence-ordered, then merged
    and re-capped — so a small ``limit`` covers marquee facts first. Source text
    is loaded only for the final selected facts (not the candidate pool).
    """
    facts: list[_Fact] = []

    tr_rows = (
        await session.execute(
            select(
                Company.id,
                Company.slug,
                Company.name,
                Company.total_raised_usd,
                Company.total_raised_as_of,
                Company.total_raised_source_url,
                Company.latest_round_amount,
            )
            .where(
                _shown(),
                Company.total_raised_usd.is_not(None),
                Company.total_raised_source_url.is_not(None),
                _has_stored_text(Company.total_raised_source_url, Company.id),
            )
            .order_by(nulls_last(Company.latest_round_amount.desc()), Company.id)
            .limit(limit)
        )
    ).all()
    for cid, slug, name, amount, as_of, url, prom in tr_rows:
        if url is None:
            continue
        facts.append(
            _Fact(
                company_id=cid,
                company_slug=slug,
                company_name=name,
                fact_kind="total_raised",
                fact_label=f"total raised {_format_usd(amount)}",
                source_url=url,
                claim=total_raised_claim(name, amount, as_of),
                prominence=float(prom) if prom is not None else 0.0,
            )
        )

    st_rows = (
        await session.execute(
            select(
                Company.id,
                Company.slug,
                Company.name,
                Company.status,
                Company.status_source_url,
                Company.latest_round_amount,
            )
            .where(
                _shown(),
                Company.status != "active",
                Company.status_source_url.is_not(None),
                _has_stored_text(Company.status_source_url, Company.id),
            )
            .order_by(nulls_last(Company.latest_round_amount.desc()), Company.id)
            .limit(limit)
        )
    ).all()
    for cid, slug, name, status, url, prom in st_rows:
        if url is None:
            continue
        facts.append(
            _Fact(
                company_id=cid,
                company_slug=slug,
                company_name=name,
                fact_kind="status",
                fact_label=f"status: {status}",
                source_url=url,
                claim=status_claim(name, status),
                prominence=float(prom) if prom is not None else 0.0,
            )
        )

    fr_rows = (
        await session.execute(
            select(
                FundingRound.company_id,
                Company.slug,
                Company.name,
                FundingRound.amount_raised,
                FundingRound.round_type,
                FundingRound.valuation_post_money,
                FundingRound.announced_date,
                FundingRound.primary_news_url,
                Company.latest_round_amount,
            )
            .select_from(FundingRound)
            .join(Company, Company.id == FundingRound.company_id)
            .where(
                _shown(),
                FundingRound.primary_news_url.is_not(None),
                _has_stored_text(FundingRound.primary_news_url, FundingRound.company_id),
            )
            .order_by(nulls_last(Company.latest_round_amount.desc()), FundingRound.id)
            .limit(limit)
        )
    ).all()
    for cid, slug, name, amount, rtype, val, adate, url, prom in fr_rows:
        if url is None:
            continue
        facts.append(
            _Fact(
                company_id=cid,
                company_slug=slug,
                company_name=name,
                fact_kind="funding_round",
                fact_label=f"{rtype or 'round'} {_format_usd(amount)}",
                source_url=url,
                claim=funding_round_claim(name, amount, rtype, val, adate),
                prominence=float(prom) if prom is not None else 0.0,
            )
        )

    # Prominence-first merge across kinds, then cap.
    facts.sort(key=lambda f: f.prominence, reverse=True)
    selected = facts[:limit]

    # Load source text for the final set only.
    for fact in selected:
        text = await _load_source_text(session, fact.company_id, fact.source_url)
        fact.source_text = truncate_to_chars(text or "", MAX_PROMPT_INPUT_CHARS)
    return [f for f in selected if len(f.source_text) >= _MIN_SOURCE_CHARS]


async def run_verify_sources(
    session: AsyncSession, *, limit: int = 25, dry_run: bool = True
) -> VerifySourcesSummary:
    """Verify a bounded, prominence-ordered slice of stored-text facts via DeepSeek.

    Dry-run only today (persists nothing); the apply path lands with migration
    0043 / ``fact_verifications``. One ``complete_json`` call per fact. A
    ``supported`` verdict whose quote is not a verbatim substring of the source
    is downgraded to ``uncertain`` and flagged (never a false ✓).
    """
    summary = VerifySourcesSummary(
        dry_run=dry_run, prompt_version=PROMPT_VERSION, results=[]
    )
    facts = await _collect_stored_text_facts(session, limit=limit)

    for fact in facts:
        summary.facts_seen += 1
        source_host = hostname(fact.source_url)
        try:
            result = await complete_json(
                build_prompt(claim=fact.claim, source_text=fact.source_text),
                SourceVerification,
            )
        except LLMError as exc:
            logger.warning(
                "verify-sources failed for %s (%s): %s",
                fact.company_slug,
                fact.fact_kind,
                exc,
            )
            summary.errors += 1
            summary.results.append(
                FactVerdict(
                    slug=fact.company_slug,
                    name=fact.company_name,
                    fact_kind=fact.fact_kind,
                    fact_label=fact.fact_label,
                    claim=fact.claim,
                    source_host=source_host,
                    verdict="uncertain",
                    grounded=False,
                    error=str(exc),
                )
            )
            continue

        verdict = result.verdict
        grounded = False
        if verdict == "supported":
            grounded = quote_is_grounded(result.supporting_quote, fact.source_text)
            if not grounded:
                # The model claimed support but the quote isn't in the source —
                # a fabrication signal. Never mark verified; downgrade to uncertain.
                summary.fabrication_flags += 1
                verdict = "uncertain"

        if verdict == "supported":
            summary.supported += 1
        elif verdict == "unsupported":
            summary.unsupported += 1
        else:
            summary.uncertain += 1

        summary.results.append(
            FactVerdict(
                slug=fact.company_slug,
                name=fact.company_name,
                fact_kind=fact.fact_kind,
                fact_label=fact.fact_label,
                claim=fact.claim,
                source_host=source_host,
                verdict=verdict,
                grounded=grounded,
                quote=result.supporting_quote if grounded else None,
            )
        )

    logger.info(
        "verify-sources: seen=%d supported=%d unsupported=%d uncertain=%d "
        "fabrication_flags=%d errors=%d dry_run=%s",
        summary.facts_seen,
        summary.supported,
        summary.unsupported,
        summary.uncertain,
        summary.fabrication_flags,
        summary.errors,
        dry_run,
    )
    return summary


def render_verify_sources_table(summary: VerifySourcesSummary) -> str:
    """Render the verify-sources run as GitHub-flavored markdown."""
    seen = summary.facts_seen
    supp_pct = (summary.supported / seen * 100) if seen else 0.0
    mode = "dry-run" if summary.dry_run else "apply"
    lines: list[str] = []
    lines.append(f"## verify-sources — {mode}")
    lines.append("")
    lines.append(f"- **Prompt version:** `{summary.prompt_version}`")
    lines.append(f"- **Facts verified:** {seen}")
    lines.append(f"- **Supported (grounded ✓):** {summary.supported} ({supp_pct:.0f}%)")
    lines.append(
        f"- **Unsupported (contradicted — data-quality signal):** {summary.unsupported}"
    )
    lines.append(f"- **Uncertain (source silent/ambiguous):** {summary.uncertain}")
    lines.append(
        f"- **Fabrication flags (supported w/ non-grounded quote — MUST be 0):** "
        f"{summary.fabrication_flags}"
    )
    lines.append(f"- **Errors:** {summary.errors}")
    lines.append("- **Cost:** see the LLM-usage block below (DeepSeek, paid).")
    lines.append("")
    lines.append("### Per-fact detail")
    lines.append("| Company | Fact | Verdict | Source | Quote / note |")
    lines.append("|---|---|---|---|---|")
    for r in summary.results:
        if r.error:
            note = f"⚠️ {r.error[:48]}"
        elif r.quote:
            note = f"“{r.quote[:64]}”"
        else:
            note = ""
        lines.append(
            f"| {r.name} | {r.fact_label} | {r.verdict} | {r.source_host} | {note} |"
        )
    if summary.dry_run:
        lines.append("")
        lines.append("### Go / no-go")
        lines.append(
            "- Meaningful **supported** rate + **zero fabrication flags** + "
            "acceptable **$/fact** → build the `fact_verifications` schema + apply "
            "path."
        )
        lines.append(
            "- Any fabrication flag, or mostly uncertain (sources don't actually "
            "state the claim), or too costly → tighten the prompt / rethink the "
            "addressable set before spending on a backfill."
        )
    return "\n".join(lines)
