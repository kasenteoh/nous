"""extract-career-history — LLM founder-background extraction (talent-flow rider).

The paid (DeepSeek) half of the talent-flow "founder background" rider. For each
shown company that has both a leadership roster (``people``) and scraped page
text, it sends ONE ``complete_json`` call over the company's concatenated
``raw_pages.content`` + roster and extracts each founder/exec's PRIOR employers
("ex-Stripe", "previously at Google"). The #184 ``career-history-probe`` found
named pedigrees are thin (~13–18% of companies), so the correct output for the
majority is an EMPTY extraction — the prompt and schema enforce empty-not-
fabricate (see ``nous.llm.prompts.career_history``).

**This module currently ships the DRY-RUN path only.** ``--dry-run`` runs the
full extraction against a bounded, prominence-ordered slice, roster-matches the
result, and renders a yield table (roster-match rate + off-roster count as a
fabrication proxy + example captured moves + the LLM $ via the usage ledger) so
a human can gate on quality BEFORE any persistence is built. It writes NOTHING
and needs NO migration — mirroring ``resolve_website_fallback``'s dry-run and the
husk-style evidence gate. The persisting apply path (DELETE+INSERT into
``career_moves``) lands with migration 0040 in a follow-up PR; calling this stage
with ``dry_run=False`` raises until then.

Cost: one call per company (~8k in + ~300 out tokens ≈ $0.0025), so a 20-company
dry run is ~$0.05 and a full ~2,600-company backfill ~$6.50 — the one owner-
approved new DeepSeek line. The exact spend is surfaced by
``observability.emit_run_telemetry`` from the ledger.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from sqlalchemy import exists, func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Person, RawPage
from nous.llm.client import MAX_PROMPT_INPUT_CHARS, LLMError, complete_json
from nous.llm.prompts.career_history import (
    PROMPT_VERSION,
    CareerHistoryExtraction,
    PriorRole,
    build_prompt,
)
from nous.util.slugify import normalize_name
from nous.util.text import extract_visible_text, truncate_to_chars

logger = logging.getLogger(__name__)

# A company needs at least this much concatenated visible text to be worth an
# LLM call — mirrors enrich-companies' guard (thin husks carry no bios).
_MIN_TEXT_CHARS = 200

# How many distinct example "moves" to surface in the summary for eyeballing.
_MAX_EXAMPLE_MOVES = 25


class CompanyCareerResult(BaseModel):
    """One company's dry-run outcome — the audit / review row."""

    slug: str
    name: str
    roster_size: int
    # People the LLM returned WITH ≥1 prior role (schema already dropped the
    # pedigree-less), split by whether the name matches the known roster.
    people_on_roster: int = 0
    people_off_roster: int = 0
    off_roster_names: list[str] = Field(default_factory=list)
    prior_role_count: int = 0  # total prior roles across roster-matched people
    # Prior roles whose "prior" company is the CURRENT company (a self-reference
    # the prompt forbids) — dropped from the yield, surfaced as a quality proxy.
    self_reference_roles: int = 0
    example_moves: list[str] = Field(default_factory=list)
    error: str | None = None


class ExtractCareerHistorySummary(BaseModel):
    """Stage summary — feeds the yield table and telemetry."""

    dry_run: bool
    prompt_version: str
    companies_seen: int = 0
    # Companies where ≥1 ROSTER-MATCHED person got ≥1 prior role (the real yield).
    companies_with_named_prior: int = 0
    total_people_with_prior: int = 0  # roster-matched only
    total_off_roster_people: int = 0  # fabrication / leakage proxy
    total_prior_roles: int = 0  # roster-matched only
    total_self_reference_roles: int = 0  # current-company-as-prior proxy
    errors: int = 0
    # Deduped roster-matched "Name: role @ PriorCo" strings for eyeballing.
    example_moves: list[str] = Field(default_factory=list)
    results: list[CompanyCareerResult] = Field(default_factory=list)


async def _load_roster(session: AsyncSession, company_id: object) -> list[tuple[str, str]]:
    """The company's known leadership as ``(name, role)`` pairs, rank-ordered."""
    rows = (
        await session.execute(
            select(Person.name, Person.role)
            .where(Person.company_id == company_id)
            .order_by(Person.rank.asc())
        )
    ).all()
    return [(name, role) for name, role in rows]


async def _load_combined_page_text(session: AsyncSession, company_id: object) -> str:
    """Concatenated visible text of all raw_pages for one company, url-ordered.

    Mirrors enrich-companies' loader so the homepage (``/``) leads the prompt.
    Uncapped here; the caller truncates to its own per-call budget.
    """
    pages = (
        (
            await session.execute(
                select(RawPage)
                .where(RawPage.company_id == company_id)
                .order_by(RawPage.url.asc())
            )
        )
        .scalars()
        .all()
    )
    parts = [extract_visible_text(page.content) for page in pages]
    return "\n\n".join(p for p in parts if p)


def _format_move(person_name: str, pr_company: str, pr_role: str | None) -> str:
    """A compact "Name: role @ PriorCo" display string for the yield table."""
    if pr_role:
        return f"{person_name}: {pr_role} @ {pr_company}"
    return f"{person_name}: {pr_company}"


def _summarize_company(
    *,
    company: Company,
    roster: list[tuple[str, str]],
    extraction: CareerHistoryExtraction,
) -> CompanyCareerResult:
    """Roster-match the LLM extraction and tally it into a review row.

    Roster-matching uses the same ``normalize_name`` key the rest of the
    codebase matches names by. Three model quirks are corrected so the yield
    numbers (the go/no-go gate) reflect real signal, not noise:

    - **Self-reference:** a prior role whose company IS the current company (the
      model echoing "founded {company}") is dropped from the count and tallied
      into ``self_reference_roles`` — the prompt forbids it, so it's a proxy.
    - **Duplicate/split people:** the LLM sometimes emits a founder twice (or as
      two name variants); people are merged by normalized name and their prior
      roles de-duplicated so one real founder counts once.
    - **Off-roster people** (advisors/investors/testimonials the prompt forbids)
      are the fabrication/leakage proxy — deduped by name, kept for the count.
    """
    roster_keys = {normalize_name(name) for name, _ in roster}
    company_key = normalize_name(company.name)
    result = CompanyCareerResult(
        slug=company.slug, name=company.name, roster_size=len(roster)
    )

    # Merge on-roster people by normalized name; dedupe their prior roles by
    # (company, role) — mirrors the intra-person dedup in PersonCareer.
    merged: dict[str, tuple[str, list[PriorRole]]] = {}  # key -> (display, roles)
    role_keys: dict[str, set[tuple[str, str]]] = {}
    off_roster_seen: set[str] = set()

    for person in extraction.people:
        key = normalize_name(person.name)
        if key in roster_keys:
            display, roles = merged.setdefault(key, (person.name, []))
            seen = role_keys.setdefault(key, set())
            for pr in person.prior_roles:
                if normalize_name(pr.company) == company_key:
                    result.self_reference_roles += 1  # current company as "prior"
                    continue
                rkey = (pr.company.lower(), (pr.role or "").lower())
                if rkey in seen:
                    continue
                seen.add(rkey)
                roles.append(pr)
        elif key and key not in off_roster_seen:
            off_roster_seen.add(key)
            result.off_roster_names.append(person.name)

    # A merged founder left with zero real prior roles (all self-references or
    # dupes) carries no signal — drop them, matching the schema's empty-drop.
    real = {k: v for k, v in merged.items() if v[1]}
    result.people_on_roster = len(real)
    result.people_off_roster = len(off_roster_seen)
    result.prior_role_count = sum(len(roles) for _, roles in real.values())
    for display, roles in real.values():
        for pr in roles:
            if len(result.example_moves) < 6:  # a few per company for the table
                result.example_moves.append(_format_move(display, pr.company, pr.role))
    return result


async def run_extract_career_history(
    session: AsyncSession,
    *,
    limit: int | None = None,
    dry_run: bool = True,
) -> ExtractCareerHistorySummary:
    """Extract founder prior-employer history for a bounded slice of companies.

    Selection: shown companies (``exclusion_reason IS NULL``) that have BOTH a
    leadership roster (≥1 ``people`` row) and ≥1 ``raw_pages`` row with enough
    text, prominence-ordered (largest raise first) so a bounded ``--limit``
    covers marquee names first. One ``complete_json`` call per company.

    ``dry_run`` (the only supported mode today) writes nothing and returns the
    roster-matched tally for the yield table. ``dry_run=False`` raises — the
    persistence path lands with migration 0040 (``career_moves``).
    """
    if not dry_run:
        raise NotImplementedError(
            "extract-career-history apply/persist path is not built yet — it "
            "lands with migration 0040 (career_moves) in a follow-up PR. Run "
            "with dry_run=True to measure extraction quality."
        )

    summary = ExtractCareerHistorySummary(dry_run=dry_run, prompt_version=PROMPT_VERSION)

    stmt = (
        select(Company)
        .where(
            Company.exclusion_reason.is_(None),
            exists().where(Person.company_id == Company.id),
            exists().where(
                RawPage.company_id == Company.id,
                func.length(RawPage.content) >= _MIN_TEXT_CHARS,
            ),
        )
        # Prominence-first, mirroring enrich / resolve-website-fallback: a
        # bounded --limit resolves the highest-raise companies first.
        .order_by(
            nulls_last(Company.latest_round_amount.desc()),
            Company.funding_round_count.desc(),
            Company.id,
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    companies = list((await session.execute(stmt)).scalars().all())

    example_seen: set[str] = set()

    for company in companies:
        summary.companies_seen += 1
        roster = await _load_roster(session, company.id)
        combined = truncate_to_chars(
            await _load_combined_page_text(session, company.id), MAX_PROMPT_INPUT_CHARS
        )
        if len(combined) < _MIN_TEXT_CHARS:
            # No usable text after visible-text extraction — skip the LLM call.
            summary.results.append(
                CompanyCareerResult(
                    slug=company.slug, name=company.name, roster_size=len(roster)
                )
            )
            continue

        prompt = build_prompt(
            company_name=company.name, roster=roster, cleaned_text=combined
        )
        try:
            extraction = await complete_json(prompt, CareerHistoryExtraction)
        except LLMError as exc:
            # Per-company LLM failure (parse, rate-limit, transport) — record and
            # keep going, never sink the whole stage. Mirrors the enrich loop.
            logger.warning("extract-career-history failed for %s: %s", company.slug, exc)
            summary.errors += 1
            summary.results.append(
                CompanyCareerResult(
                    slug=company.slug,
                    name=company.name,
                    roster_size=len(roster),
                    error=str(exc),
                )
            )
            continue

        result = _summarize_company(
            company=company, roster=roster, extraction=extraction
        )
        summary.results.append(result)
        summary.total_people_with_prior += result.people_on_roster
        summary.total_off_roster_people += result.people_off_roster
        summary.total_prior_roles += result.prior_role_count
        summary.total_self_reference_roles += result.self_reference_roles
        if result.people_on_roster > 0:
            summary.companies_with_named_prior += 1
        # Accumulate distinct example moves for the summary-level list.
        for move in result.example_moves:
            key = move.lower()
            if key not in example_seen and len(summary.example_moves) < _MAX_EXAMPLE_MOVES:
                example_seen.add(key)
                summary.example_moves.append(move)

    logger.info(
        "extract-career-history: seen=%d with_named_prior=%d people=%d "
        "prior_roles=%d off_roster=%d self_ref=%d errors=%d dry_run=%s",
        summary.companies_seen,
        summary.companies_with_named_prior,
        summary.total_people_with_prior,
        summary.total_prior_roles,
        summary.total_off_roster_people,
        summary.total_self_reference_roles,
        summary.errors,
        dry_run,
    )
    return summary


def render_yield_table(summary: ExtractCareerHistorySummary) -> str:
    """Render the dry-run yield table as GitHub-flavored markdown.

    Reports the roster-matched yield (companies with ≥1 named prior, people,
    prior roles), the off-roster count (the fabrication/leakage proxy — the
    prompt forbids off-roster people, so any are a quality flag), example moves
    for eyeballing, a per-company detail table, and a go/no-go guide. Cost lands
    in the separate ``emit_run_telemetry`` block.
    """
    seen = summary.companies_seen
    with_prior_pct = (summary.companies_with_named_prior / seen * 100) if seen else 0.0
    example_moves = summary.example_moves
    lines: list[str] = []
    lines.append("## extract-career-history — dry-run yield")
    lines.append("")
    lines.append(f"- **Prompt version:** `{summary.prompt_version}`")
    lines.append(f"- **Companies processed:** {seen}")
    lines.append(
        f"- **With ≥1 named prior (roster-matched):** "
        f"{summary.companies_with_named_prior} ({with_prior_pct:.0f}%)"
    )
    lines.append(
        f"- **Founders/execs with a prior employer:** {summary.total_people_with_prior}"
    )
    lines.append(f"- **Prior-role edges extracted:** {summary.total_prior_roles}")
    lines.append(
        f"- **Off-roster people (fabrication/leakage proxy):** "
        f"{summary.total_off_roster_people}"
    )
    lines.append(
        f"- **Self-reference roles dropped (current-company-as-prior proxy):** "
        f"{summary.total_self_reference_roles}"
    )
    lines.append(f"- **Errors:** {summary.errors}")
    lines.append("- **Cost:** see the LLM-usage block below (DeepSeek, paid).")
    lines.append("")
    if example_moves:
        moves = "\n".join(f"- `{m}`" for m in example_moves)
        lines.append("### Example captured moves (roster-matched)")
        lines.append(moves)
        lines.append("")
    lines.append("### Per-company detail")
    lines.append("| Company | Roster | On-roster | Prior roles | Off-roster | Note |")
    lines.append("|---|--:|--:|--:|--:|---|")
    for r in summary.results:
        note = ""
        if r.error:
            note = f"⚠️ {r.error[:40]}"
        elif r.off_roster_names:
            note = "off-roster: " + ", ".join(r.off_roster_names[:3])
        elif r.self_reference_roles:
            note = f"{r.self_reference_roles} self-ref dropped"
        lines.append(
            f"| {r.name} | {r.roster_size} | {r.people_on_roster} "
            f"| {r.prior_role_count} | {r.people_off_roster} | {note} |"
        )
    lines.append("")
    lines.append("### Go / no-go")
    lines.append(
        "- Clean named employers on the roster + empty where the bio states no "
        "pedigree + **near-zero off-roster and self-reference** → build the "
        "persisting pipeline."
    )
    lines.append(
        "- Many off-roster names, fabricated/ambiguous employers, or heavy "
        "self-reference → tighten the prompt and re-run before the backfill."
    )
    return "\n".join(lines)
