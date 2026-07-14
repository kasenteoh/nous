r"""career-history-probe — $0, read-only feasibility diagnostic.

Measures whether the scraped team/about prose in ``raw_pages.content`` carries
NAMED prior-employer mentions ("ex-Stripe", "previously at Google") — the gate
before any LLM spend on a talent-flow feature. It EXTRACTS nothing, writes
nothing, uses NO LLM, and needs NO migration. It is the third read-only
instrument on the DB (alongside db-stats for *size* and data-quality for
*completeness*): a phrase-regex census over the *shown* cohort
(``exclusion_reason IS NULL``) with at least one scraped page.

Two measurement tiers, both $0:

**A. Full-cohort SQL aggregate** — POSTGRES case-insensitive regex (``~*``) run
inside the database as ``COUNT(DISTINCT company_id)`` (near-zero data transfer):
  - ``shown_companies_with_pages``      — the denominator.
  - ``companies_with_bio_section``      — a leadership/team section exists at all.
  - ``companies_with_any_career_signal``— Tier-1 (broad "mentions a past") hit.
  - ``companies_with_named_prior_company`` — Tier-2 (a capitalized employer token
    follows a career-transition cue). **This is the headline number.**
  - a per-Tier-1-phrase company-count histogram.

**B. Prominence sample** — the top ``--sample`` shown companies by
``latest_round_amount`` (the marquee cohort the feature most needs to work for):
concatenate their pages, run the Tier-2 regex IN PYTHON, and report the
precision-corrected named-prior rate plus a handful of example captured employer
strings so a human can eyeball whether the regex catches real "ex-Stripe"-style
mentions and not noise like "at scale".

Regex portability (the SQL-side ``~*`` and the Python-side ``re`` patterns are
the SAME module constants, so they cannot drift): the constants deliberately
avoid engine-specific tokens. In particular they never use ``\b`` — a word
boundary in Python but the *backspace* character in POSTGRES ARE — expressing
boundaries with explicit ``(?:^|[^a-z])`` / ``[^a-z]`` character classes that
both engines fold identically under case-insensitive matching. ``\d \w \s \W``,
non-capturing groups, lazy quantifiers and ``^``/``$`` behave the same in both.

One inherent asymmetry, documented on purpose: the SAME Tier-2 pattern string is
run two ways. At the DB it goes through ``~*`` (case-insensitive), so the ``[A-Z]``
employer initial folds and matches lowercase too — the SQL headline is an *upper
bound*. In Python the sample compiles it CASE-SENSITIVELY, so ``[A-Z]`` is
honored and the captured employer must genuinely start uppercase ("a named org,
not 'a large fintech'"). The cue words carry case-tolerant initials (``[Pp]``…)
so a sentence-start "Previously" still matches under case-sensitive Python. So
``companies_with_named_prior_company`` is the ceiling and ``sample_named_prior_rate``
is the precision-corrected figure — reading the two together is the whole design.
(Tier-1 and the bio marker have no capital-precision concern, so their Python
views use IGNORECASE for exact parity with their own ``~*`` counts.)

No writes, no ``pipeline_runs`` row (mirrors db-stats / data-quality).
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import distinct, exists, func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.observability import write_step_summary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex constants — shared by the SQL (``~*``) and Python (``re``) paths.
#
# Every pattern below is valid and equivalent in BOTH POSTGRES ARE and Python
# ``re`` under case-insensitive matching. See the module docstring for the
# portability rules (no ``\b``; boundaries via ``(?:^|[^a-z])``/``[^a-z]``).
# ---------------------------------------------------------------------------

# Tier 1 — career-history SIGNAL (broad; any hit ⇒ the bio mentions a past).
# ``(label, pattern)`` pairs: the labels key the per-phrase histogram, the
# patterns are OR-ed into TIER1_COMBINED for the "any signal" count.
TIER1_PHRASES: list[tuple[str, str]] = [
    ("previously", r"previously"),
    ("formerly", r"formerly"),
    ("prior to", r"prior to"),
    ("before founding/starting/joining", r"before (?:founding|starting|joining)"),
    ("worked at", r"worked at"),
    ("spent N years", r"spent \d+ years"),
    ("N years at", r"\d+ years at"),
    ("veteran of", r"veteran of"),
    ("early employee/engineer/hire at", r"early (?:employee|engineer|hire) at"),
    ("led … at", r"led .{0,40} at"),
    ("built … at", r"built .{0,40} at"),
    # ``ex-`` only when not preceded by a letter (skips "flex-", "complex-") and
    # followed by a letter (a name/word), e.g. "ex-Google". Under ``~*`` /
    # IGNORECASE, ``[^a-z]`` and ``[a-z]`` fold to exclude/include A–Z too.
    ("ex-<Name>", r"(?:^|[^a-z])ex-[a-z]"),
    ("co-founded", r"co-?founded"),
]

# Combined Tier-1 alternation for the single "any career signal" count.
TIER1_COMBINED: str = "|".join(f"(?:{pattern})" for _, pattern in TIER1_PHRASES)

# Tier 2 — NAMED prior-employer proxy (the real gate). A career-transition cue,
# then optionally a few lowercase words and/or an "at"/"@" connector, then a
# capitalized employer token (up to three words). ``\W*`` after the cue (not
# ``\W+``) lets the cue abut the name, so "ex-Stripe" captures "Stripe". The
# capital-initial ``[A-Z]`` is the precision lever: Python compiles this
# CASE-SENSITIVELY so the employer must truly start uppercase, while ``~*`` at
# the DB folds it away (upper bound). Cue initials are case-tolerant (``[Pp]``…)
# so a sentence-start "Previously" still matches under case-sensitive Python.
TIER2_NAMED_PRIOR: str = (
    r"(?:[Pp]reviously|[Ff]ormerly|[Pp]rior to|"
    r"[Bb]efore (?:founding|starting|joining)|"
    r"[Ee]x-|[Vv]eteran of|[Ss]pent \d+ years|"
    r"[Ee]arly (?:employee|engineer|hire))"
    r"\W*(?:[a-z]+\W+){0,6}?(?:(?:at|@)\W+)?"
    r"([A-Z][A-Za-z0-9&-]+(?:\s+[A-Z][A-Za-z0-9&-]+){0,2})"
)

# Bio-presence marker — is there even a leadership/team section to mine? The
# acronyms carry explicit non-letter boundaries so "coo" in "cooperate" or
# "ceo" mid-word never register a false section.
BIO_MARKER: str = (
    r"(?:co-?founders?|founders?|chief \w+ officer|head of|our team|leadership|"
    r"the team|(?:^|[^a-z])(?:CEO|CTO|COO)(?:[^a-z]|$))"
)

# Pre-compiled Python views of the shared constants. Tier-1 and the bio marker
# use IGNORECASE for exact parity with their ``~*`` counts. Tier-2 is compiled
# CASE-SENSITIVELY on purpose — the ``[A-Z]`` employer initial is the precision
# lever, and its cue initials are already case-tolerant (``[Pp]``…).
_TIER1_COMBINED_RE = re.compile(TIER1_COMBINED, re.IGNORECASE)
_TIER2_NAMED_PRIOR_RE = re.compile(TIER2_NAMED_PRIOR)
_BIO_MARKER_RE = re.compile(BIO_MARKER, re.IGNORECASE)

# Cap the per-company text a single Python regex scan sees, so the bounded-but-
# non-trivial Tier-2 pattern can't blow up on a pathologically large blob. Only
# affects the sample (the SQL path scans full content in-database).
_SAMPLE_SCAN_CHAR_CAP = 500_000

# How many distinct example captures to surface for human eyeballing.
_MAX_EXAMPLE_CAPTURES = 20


def has_career_signal(text: str) -> bool:
    """True when *text* carries any Tier-1 career-history signal."""
    return _TIER1_COMBINED_RE.search(text) is not None


def has_bio_section(text: str) -> bool:
    """True when *text* looks like it has a leadership/team section to mine."""
    return _BIO_MARKER_RE.search(text) is not None


def capture_prior_employers(text: str) -> list[str]:
    """Return capitalized employer tokens that follow a career-transition cue.

    The precision half of the diagnostic. Matching is CASE-SENSITIVE, so the
    ``[A-Z]`` name tokens capture only genuinely capitalized employers ("a named
    org, not 'a large fintech'"); the pattern's case-tolerant cue initials still
    let a sentence-start "Previously" register. The ``isupper`` guard below is a
    belt-and-suspenders check. Order-preserving, de-duplicated case-insensitively.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _TIER2_NAMED_PRIOR_RE.finditer(text):
        candidate = match.group(1).strip()
        if not candidate or not candidate[:1].isupper():
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def has_named_prior(text: str) -> bool:
    """True when *text* yields at least one capital-initial prior employer."""
    return bool(capture_prior_employers(text))


class CareerHistoryProbeSummary(BaseModel):
    """Result of one career-history-probe run (all counts over the shown cohort)."""

    # Full-cohort SQL aggregate (Tier A).
    shown_companies_with_pages: int  # denominator
    companies_with_bio_section: int
    companies_with_any_career_signal: int  # Tier-1
    companies_with_named_prior_company: int  # Tier-2 headline (upper bound)
    bio_section_pct: float
    any_career_signal_pct: float
    named_prior_pct: float
    per_phrase_company_counts: dict[str, int]  # Tier-1 histogram

    # Prominence sample (Tier B) — precision-corrected in Python.
    sample_size: int
    sample_named_prior_rate: float  # fraction 0..1 of sampled companies
    sample_example_captures: list[str]


def _pct(numerator: int, denominator: int) -> float:
    """Percentage of *numerator* over *denominator*, or 0.0 when empty."""
    return round(numerator / denominator * 100, 1) if denominator else 0.0


async def _count_shown_companies_with_page_matching(
    session: AsyncSession, pattern: str | None
) -> int:
    """COUNT(DISTINCT company_id) of shown companies with a page matching *pattern*.

    ``pattern`` is applied via the POSTGRES case-insensitive regex operator
    (``~*``) — an ORM operator, not raw SQL. ``None`` counts the denominator
    (any page). A company is counted when it has at least one matching page.
    """
    stmt = (
        select(func.count(distinct(RawPage.company_id)))
        .select_from(RawPage)
        .join(Company, Company.id == RawPage.company_id)
        .where(Company.exclusion_reason.is_(None))
    )
    if pattern is not None:
        stmt = stmt.where(RawPage.content.op("~*")(pattern))
    return (await session.execute(stmt)).scalar_one()


async def run_career_history_probe(
    session: AsyncSession, *, sample: int
) -> CareerHistoryProbeSummary:
    """Census the shown cohort for named prior-employer signal; return a summary.

    Read-only and idempotent. Tier A runs SQL ``COUNT(DISTINCT company_id)``
    aggregates; Tier B pulls the top ``sample`` shown companies by funding and
    runs the Tier-2 regex in Python for a precision-corrected rate.
    """
    # --- Tier A: full-cohort SQL aggregates ---------------------------------
    denominator = await _count_shown_companies_with_page_matching(session, None)
    bio = await _count_shown_companies_with_page_matching(session, BIO_MARKER)
    any_signal = await _count_shown_companies_with_page_matching(
        session, TIER1_COMBINED
    )
    named_prior = await _count_shown_companies_with_page_matching(
        session, TIER2_NAMED_PRIOR
    )

    per_phrase: dict[str, int] = {}
    for label, pattern in TIER1_PHRASES:
        per_phrase[label] = await _count_shown_companies_with_page_matching(
            session, pattern
        )

    # --- Tier B: prominence sample (top-N by latest round) ------------------
    sample_ids: list[UUID] = []
    if sample > 0:
        sample_ids = list(
            (
                await session.execute(
                    select(Company.id)
                    .where(Company.exclusion_reason.is_(None))
                    .where(exists().where(RawPage.company_id == Company.id))
                    .order_by(nulls_last(Company.latest_round_amount.desc()))
                    .limit(sample)
                )
            )
            .scalars()
            .all()
        )

    # Fetch all pages for the sampled companies in one query, url-ordered so the
    # homepage leads each company's concatenated text (mirrors enrich's concat).
    pages_by_company: dict[UUID, list[str]] = {}
    if sample_ids:
        rows = (
            await session.execute(
                select(RawPage.company_id, RawPage.content)
                .where(RawPage.company_id.in_(sample_ids))
                .order_by(RawPage.company_id, RawPage.url)
            )
        ).all()
        for company_id, content in rows:
            pages_by_company.setdefault(company_id, []).append(content or "")

    named_hits = 0
    example_captures: list[str] = []
    example_seen: set[str] = set()
    for company_id in sample_ids:
        combined = "\n\n".join(pages_by_company.get(company_id, []))[
            :_SAMPLE_SCAN_CHAR_CAP
        ]
        captures = capture_prior_employers(combined)
        if captures:
            named_hits += 1
        for cap in captures:
            key = cap.lower()
            if key not in example_seen and len(example_captures) < _MAX_EXAMPLE_CAPTURES:
                example_seen.add(key)
                example_captures.append(cap)

    sample_size = len(sample_ids)
    sample_rate = named_hits / sample_size if sample_size else 0.0

    return CareerHistoryProbeSummary(
        shown_companies_with_pages=denominator,
        companies_with_bio_section=bio,
        companies_with_any_career_signal=any_signal,
        companies_with_named_prior_company=named_prior,
        bio_section_pct=_pct(bio, denominator),
        any_career_signal_pct=_pct(any_signal, denominator),
        named_prior_pct=_pct(named_prior, denominator),
        per_phrase_company_counts=per_phrase,
        sample_size=sample_size,
        sample_named_prior_rate=round(sample_rate, 4),
        sample_example_captures=example_captures,
    )


def emit_career_history_probe_summary(summary: CareerHistoryProbeSummary) -> None:
    """Append the probe report as a GitHub Actions step-summary (mirrors db-stats)."""
    denom = summary.shown_companies_with_pages
    lines: list[str] = []
    lines.append("## Career-history feasibility probe")
    lines.append("")
    lines.append(
        f"**Named prior-company estimate: {summary.companies_with_named_prior_company} "
        f"of {denom} shown companies ({summary.named_prior_pct:.1f}%) would yield "
        f"≥1 named prior employer.**"
    )
    lines.append("")
    lines.append(
        "Cohort: shown companies (`exclusion_reason IS NULL`) with ≥1 scraped "
        "page. Read-only, no LLM, no writes."
    )
    lines.append("")
    lines.append("### Full-cohort signal (SQL `~*` over raw_pages.content)")
    lines.append("| Signal | Companies | % of cohort |")
    lines.append("|---|--:|--:|")
    lines.append(f"| shown companies with pages (denominator) | {denom} | 100.0% |")
    lines.append(
        f"| have a bio / leadership section | {summary.companies_with_bio_section} "
        f"| {summary.bio_section_pct:.1f}% |"
    )
    lines.append(
        f"| any career-history signal (Tier 1) | "
        f"{summary.companies_with_any_career_signal} "
        f"| {summary.any_career_signal_pct:.1f}% |"
    )
    lines.append(
        f"| **named prior employer (Tier 2, headline)** | "
        f"**{summary.companies_with_named_prior_company}** "
        f"| **{summary.named_prior_pct:.1f}%** |"
    )
    lines.append("")
    lines.append("### Tier-1 phrase histogram")
    lines.append("| Phrase | Companies |")
    lines.append("|---|--:|")
    for label, _ in TIER1_PHRASES:
        lines.append(f"| {label} | {summary.per_phrase_company_counts.get(label, 0)} |")
    lines.append("")
    lines.append(
        "### Prominence sample (top-N by latest round, Tier-2 regex in Python)"
    )
    lines.append(f"- sample size: **{summary.sample_size}**")
    lines.append(
        f"- named-prior rate (capital-initial filtered): "
        f"**{summary.sample_named_prior_rate * 100:.1f}%**"
    )
    if summary.sample_example_captures:
        examples = ", ".join(f"`{c}`" for c in summary.sample_example_captures)
        lines.append(f"- example captured employers: {examples}")
    else:
        lines.append("- example captured employers: _(none)_")
    lines.append("")
    lines.append("### Go / no-go")
    lines.append(
        "- named-prior rate **≳30–40%** → green-light a bounded LLM extraction "
        "dry run"
    )
    lines.append(
        "- **~10–25%** → thin; a niche \"notable alumni\" surface only, weigh cost"
    )
    lines.append("- **<10%** → the LLM pass mostly returns empty; don't spend")
    write_step_summary("\n".join(lines))
