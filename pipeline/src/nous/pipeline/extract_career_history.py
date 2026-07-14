"""extract-career-history — LLM founder-background extraction (talent-flow rider).

The paid (DeepSeek) half of the talent-flow "founder background / notable alumni"
rider. For each shown company that has both a leadership roster (``people``) and
scraped page text AND has not yet been extracted at the current prompt version,
it sends ONE ``complete_json`` call over the company's concatenated
``raw_pages.content`` + roster and extracts each founder/exec's PRIOR employers
("ex-Stripe", "previously at Google"). The #184 ``career-history-probe`` found
named pedigrees are thin (~13-18% of companies), so the correct output for the
majority is an EMPTY extraction — the prompt and schema enforce empty-not-
fabricate (see ``nous.llm.prompts.career_history``).

Two modes:

- ``--dry-run`` (the #185 evidence gate) runs the extraction over a bounded,
  prominence-ordered slice, roster-matches the result, and renders a yield table
  (roster-match rate + off-roster / self-reference fabrication proxies + example
  moves + the LLM $ via ``emit_run_telemetry``). Writes NOTHING.
- apply (default) additionally PERSISTS, replace-style per company: DELETE the
  company's ``career_moves`` rows, INSERT the freshly extracted edges (with
  ``prior_company_id`` resolved to the catalog where the verbatim name uniquely
  matches), and stamp ``companies.career_extracted_prompt_version`` so the
  company is not re-extracted (re-billed) until the prompt version bumps. The
  stamp is what makes the ~85% empty-bio companies idempotent — ``career_moves``
  rows alone can't tell "never extracted" from "extracted, correctly empty".

Selection is version-gated (``career_extracted_prompt_version IS NULL OR
< PROMPT_VERSION``, mirroring ``--redescribe-outdated``) and ``--limit`` bounded,
so a bounded run drains the backlog and a prompt bump re-selects everyone.

Cost: one call per company (~8k in + ~300 out tokens ≈ $0.0025 — the #185 dry run
measured $0.0013/company), so the one-time backfill of the ~2,600-company cohort
is ~$3-6.50 — the one owner-approved new DeepSeek line. The exact spend is
surfaced by ``observability.emit_run_telemetry`` from the ledger.
"""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, func, nulls_last, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from nous.db.models import CareerMove, Company, Person, RawPage
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

# A few example moves per company in the per-company detail table.
_EXAMPLES_PER_COMPANY = 6


class _PersistMove(BaseModel):
    """One deduplicated (person → prior company) edge, ready to become a row.

    Collapsed to one edge per (person, prior company): the ``career_moves``
    unique key is (company_id, person_normalized_name, prior_company_name) and
    excludes the role, so two titles at the same prior employer (a promotion)
    are stored as a single edge carrying the first-mentioned role.
    """

    person_name: str
    person_normalized_name: str
    prior_company_name: str
    prior_role: str | None
    start_year: int | None
    end_year: int | None


class CompanyCareerResult(BaseModel):
    """One company's outcome — the audit / review row (dry-run and apply)."""

    slug: str
    name: str
    roster_size: int
    # Distinct roster-matched founders/execs with ≥1 (deduped) prior-employer edge.
    people_on_roster: int = 0
    people_off_roster: int = 0
    off_roster_names: list[str] = Field(default_factory=list)
    # Deduplicated (person → prior company) edges — one persisted row each.
    edge_count: int = 0
    # Prior roles whose "prior" company is the CURRENT company (a self-reference
    # the prompt forbids) — dropped from the yield, surfaced as a quality proxy.
    self_reference_roles: int = 0
    example_moves: list[str] = Field(default_factory=list)
    error: str | None = None


class _CompanyExtraction(BaseModel):
    """Per-company computed result plus the edges to persist (apply mode)."""

    result: CompanyCareerResult
    moves: list[_PersistMove] = Field(default_factory=list)


class ExtractCareerHistorySummary(BaseModel):
    """Stage summary — feeds the yield table, telemetry, and pipeline_runs."""

    dry_run: bool
    prompt_version: str
    companies_seen: int = 0
    # Companies where ≥1 ROSTER-MATCHED person got ≥1 prior-employer edge.
    companies_with_named_prior: int = 0
    total_people_with_prior: int = 0  # roster-matched only
    total_off_roster_people: int = 0  # fabrication / leakage proxy
    total_edges: int = 0  # deduped (person → prior company) edges
    total_self_reference_roles: int = 0  # current-company-as-prior proxy
    errors: int = 0
    # Apply-mode counters (0 in dry-run).
    rows_written: int = 0
    companies_stamped: int = 0
    prior_company_ids_resolved: int = 0
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


def _compute_company(
    *,
    company: Company,
    roster: list[tuple[str, str]],
    extraction: CareerHistoryExtraction,
) -> _CompanyExtraction:
    """Roster-match + dedupe the LLM extraction into review counts + edges.

    Three model quirks are corrected so the yield numbers and persisted edges
    reflect real signal, not noise:

    - **Self-reference:** a prior role whose company IS the current company (the
      model echoing "founded {company}") is dropped and tallied as a proxy.
    - **Duplicate/split people:** the LLM sometimes emits a founder twice; people
      are merged by normalized name.
    - **Off-roster people** (advisors/investors/testimonials the prompt forbids)
      are the fabrication/leakage proxy — deduped by name, kept for the count.

    Edges are then collapsed to one per (person, prior company): the persisted
    ``career_moves`` unique key excludes the role, so a promotion at one employer
    is a single edge carrying the first-mentioned role.
    """
    roster_keys = {normalize_name(name) for name, _ in roster}
    company_key = normalize_name(company.name)
    result = CompanyCareerResult(
        slug=company.slug, name=company.name, roster_size=len(roster)
    )

    # Merge on-roster people by normalized name; dedupe their prior roles by
    # (company, role); track off-roster names (deduped) as the fabrication proxy.
    merged: dict[str, tuple[str, list[PriorRole]]] = {}  # key -> (display, roles)
    role_seen: dict[str, set[tuple[str, str]]] = {}
    off_roster_seen: set[str] = set()

    for person in extraction.people:
        key = normalize_name(person.name)
        if key in roster_keys:
            display, roles = merged.setdefault(key, (person.name, []))
            seen = role_seen.setdefault(key, set())
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

    # Collapse to one edge per (person, prior company); first role wins.
    moves: list[_PersistMove] = []
    people_with_edges: set[str] = set()
    for key, (display, roles) in merged.items():
        edge_seen: set[str] = set()
        for pr in roles:
            ekey = pr.company.lower()
            if ekey in edge_seen:
                continue
            edge_seen.add(ekey)
            people_with_edges.add(key)
            moves.append(
                _PersistMove(
                    person_name=display,
                    person_normalized_name=key,
                    prior_company_name=pr.company,
                    prior_role=pr.role,
                    start_year=pr.start_year,
                    end_year=pr.end_year,
                )
            )

    result.people_on_roster = len(people_with_edges)
    result.people_off_roster = len(off_roster_seen)
    result.edge_count = len(moves)
    for move in moves[:_EXAMPLES_PER_COMPANY]:
        result.example_moves.append(
            _format_move(move.person_name, move.prior_company_name, move.prior_role)
        )
    return _CompanyExtraction(result=result, moves=moves)


async def _resolve_prior_company_id(
    session: AsyncSession,
    normalized_prior: str,
    *,
    current_company_id: UUID,
    cache: dict[str, UUID | None],
) -> UUID | None:
    """Resolve a verbatim prior-company name to a catalog company id, or None.

    High-precision by design (the moat prefers a missing link to a wrong one):
    an EXACT ``normalize_name`` match against a SHOWN company, and only when that
    match is UNIQUE — 0 or ≥2 matches (or the current company itself) resolve to
    None. Most prior employers (Intel, IBM, NVIDIA) aren't catalogued and stay
    NULL; the occasional in-catalog startup-to-startup edge is the payoff.
    Cached per run (the same employer recurs across companies).
    """
    if not normalized_prior:
        return None
    if normalized_prior in cache:
        resolved = cache[normalized_prior]
    else:
        ids = (
            (
                await session.execute(
                    select(Company.id)
                    .where(
                        Company.normalized_name == normalized_prior,
                        Company.exclusion_reason.is_(None),
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        resolved = ids[0] if len(ids) == 1 else None
        cache[normalized_prior] = resolved
    # Never link a move back to its own company (defensive — self-refs are
    # already name-filtered upstream, but an exact match could coincide).
    return None if resolved == current_company_id else resolved


async def _persist_company(
    session: AsyncSession,
    *,
    company: Company,
    moves: list[_PersistMove],
    resolve_cache: dict[str, UUID | None],
) -> tuple[int, int]:
    """Replace-style write of one company's career_moves; stamp + commit.

    DELETE the company's existing rows then INSERT the fresh edge set (even when
    empty — that clears stale rows from an older prompt version), resolve each
    edge's ``prior_company_id``, and stamp the company. Returns
    ``(rows_written, prior_ids_resolved)``. Raises on DB error (caller handles).
    """
    await session.execute(
        delete(CareerMove).where(CareerMove.company_id == company.id)
    )
    resolved_count = 0
    for move in moves:
        prior_id = await _resolve_prior_company_id(
            session,
            normalize_name(move.prior_company_name),
            current_company_id=company.id,
            cache=resolve_cache,
        )
        if prior_id is not None:
            resolved_count += 1
        session.add(
            CareerMove(
                company_id=company.id,
                person_name=move.person_name,
                person_normalized_name=move.person_normalized_name,
                prior_company_name=move.prior_company_name,
                prior_company_id=prior_id,
                prior_role=move.prior_role,
                start_year=move.start_year,
                end_year=move.end_year,
                # Provenance: the company's own site is the source of its bios
                # (mirrors how enrich stamps people.source_url = website).
                source_url=company.website,
                extraction_prompt_version=PROMPT_VERSION,
            )
        )
    company.career_extracted_prompt_version = PROMPT_VERSION
    await session.commit()
    return len(moves), resolved_count


async def run_extract_career_history(
    session: AsyncSession,
    *,
    limit: int | None = None,
    dry_run: bool = True,
) -> ExtractCareerHistorySummary:
    """Extract (and, unless dry-run, persist) founder prior-employer history.

    Selection: shown companies (``exclusion_reason IS NULL``) that have BOTH a
    leadership roster (≥1 ``people`` row) and ≥1 ``raw_pages`` row with enough
    text, and whose ``career_extracted_prompt_version`` is NULL or below the
    current ``PROMPT_VERSION`` (version-gated idempotency), prominence-ordered so
    a bounded ``--limit`` covers marquee names first. One ``complete_json`` call
    per company.

    ``dry_run`` writes nothing and returns the roster-matched tally for the yield
    table. Apply (``dry_run=False``) additionally DELETE+INSERTs each company's
    ``career_moves`` replace-style, resolves ``prior_company_id``, and stamps the
    company (including one that correctly extracted zero edges — so empties are
    not re-billed). Per-company commit; a transient DB error leaves the company
    un-stamped (re-eligible next run).
    """
    summary = ExtractCareerHistorySummary(dry_run=dry_run, prompt_version=PROMPT_VERSION)

    stmt = (
        # Select IDs, not ORM objects: a per-company ROLLBACK (StaleDataError /
        # IntegrityError below) expires the WHOLE identity map regardless of
        # expire_on_commit, so touching a preloaded Company after a prior
        # rollback would fire sync IO (MissingGreenlet) and crash the run. We
        # re-`session.get` each company fresh at the top of the loop instead —
        # greenlet-safe, and it also skips a row merged away since selection.
        select(Company.id)
        .where(
            Company.exclusion_reason.is_(None),
            exists().where(Person.company_id == Company.id),
            exists().where(
                RawPage.company_id == Company.id,
                func.length(RawPage.content) >= _MIN_TEXT_CHARS,
            ),
            or_(
                Company.career_extracted_prompt_version.is_(None),
                Company.career_extracted_prompt_version < PROMPT_VERSION,
            ),
        )
        # Prominence-first, mirroring enrich / resolve-website-fallback.
        .order_by(
            nulls_last(Company.latest_round_amount.desc()),
            Company.funding_round_count.desc(),
            Company.id,
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    company_ids = list((await session.execute(stmt)).scalars().all())

    resolve_cache: dict[str, UUID | None] = {}
    example_seen: set[str] = set()

    for company_id in company_ids:
        company = await session.get(Company, company_id)
        if company is None:
            continue  # merged/deleted between selection and now
        summary.companies_seen += 1
        # Capture the slug NOW (while the object is fresh): a per-company
        # rollback below expires every ORM attribute, so the post-rollback log
        # lines must use this local, not company.slug (which would fire sync IO).
        company_slug = company.slug
        roster = await _load_roster(session, company.id)
        combined = truncate_to_chars(
            await _load_combined_page_text(session, company.id), MAX_PROMPT_INPUT_CHARS
        )
        if len(combined) < _MIN_TEXT_CHARS:
            # No usable text after visible-text extraction. In apply mode stamp
            # it as extracted-empty (so it isn't re-billed) and clear any stale
            # rows; write nothing else.
            result = CompanyCareerResult(
                slug=company.slug, name=company.name, roster_size=len(roster)
            )
            summary.results.append(result)
            if not dry_run:
                try:
                    await _persist_company(
                        session, company=company, moves=[], resolve_cache=resolve_cache
                    )
                    summary.companies_stamped += 1
                except (StaleDataError, IntegrityError):
                    await session.rollback()
                    summary.errors += 1
            continue

        prompt = build_prompt(
            company_name=company.name, roster=roster, cleaned_text=combined
        )
        try:
            extraction = await complete_json(prompt, CareerHistoryExtraction)
        except LLMError as exc:
            # Per-company LLM failure — record and keep going, never sink the
            # stage. Leave the company un-stamped so it stays eligible next run.
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

        computed = _compute_company(company=company, roster=roster, extraction=extraction)
        result = computed.result
        summary.results.append(result)
        summary.total_people_with_prior += result.people_on_roster
        summary.total_off_roster_people += result.people_off_roster
        summary.total_edges += result.edge_count
        summary.total_self_reference_roles += result.self_reference_roles
        if result.people_on_roster > 0:
            summary.companies_with_named_prior += 1
        for move in result.example_moves:
            key = move.lower()
            if key not in example_seen and len(summary.example_moves) < _MAX_EXAMPLE_MOVES:
                example_seen.add(key)
                summary.example_moves.append(move)

        if not dry_run:
            try:
                rows, resolved = await _persist_company(
                    session,
                    company=company,
                    moves=computed.moves,
                    resolve_cache=resolve_cache,
                )
                summary.rows_written += rows
                summary.prior_company_ids_resolved += resolved
                summary.companies_stamped += 1
            except StaleDataError:
                await session.rollback()
                logger.warning(
                    "Company %s disappeared mid-extract (concurrent merge) — skipping.",
                    company_slug,
                )
                summary.errors += 1
            except IntegrityError:
                # Deduping should prevent unique-key collisions; treat any that
                # slip through as a per-company error rather than sinking the run.
                await session.rollback()
                logger.exception("career_moves write failed for %s", company_slug)
                summary.errors += 1

    logger.info(
        "extract-career-history: seen=%d with_named_prior=%d people=%d edges=%d "
        "off_roster=%d self_ref=%d rows_written=%d stamped=%d resolved=%d "
        "errors=%d dry_run=%s",
        summary.companies_seen,
        summary.companies_with_named_prior,
        summary.total_people_with_prior,
        summary.total_edges,
        summary.total_off_roster_people,
        summary.total_self_reference_roles,
        summary.rows_written,
        summary.companies_stamped,
        summary.prior_company_ids_resolved,
        summary.errors,
        dry_run,
    )
    return summary


def render_yield_table(summary: ExtractCareerHistorySummary) -> str:
    """Render the run summary as GitHub-flavored markdown.

    Dry-run: the yield + fabrication proxies + a go/no-go guide. Apply: the same
    yield plus the persistence counters (rows written, companies stamped,
    prior_company_id links resolved). Cost lands in the ``emit_run_telemetry``
    block.
    """
    seen = summary.companies_seen
    with_prior_pct = (summary.companies_with_named_prior / seen * 100) if seen else 0.0
    mode = "dry-run" if summary.dry_run else "apply"
    lines: list[str] = []
    lines.append(f"## extract-career-history — {mode}")
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
    lines.append(f"- **Prior-employer edges:** {summary.total_edges}")
    lines.append(
        f"- **Off-roster people (fabrication/leakage proxy):** "
        f"{summary.total_off_roster_people}"
    )
    lines.append(
        f"- **Self-reference roles dropped (current-company-as-prior proxy):** "
        f"{summary.total_self_reference_roles}"
    )
    lines.append(f"- **Errors:** {summary.errors}")
    if not summary.dry_run:
        lines.append(
            f"- **Persisted:** {summary.rows_written} rows across "
            f"{summary.companies_stamped} stamped companies; "
            f"{summary.prior_company_ids_resolved} in-catalog links resolved."
        )
    lines.append("- **Cost:** see the LLM-usage block below (DeepSeek, paid).")
    lines.append("")
    if summary.example_moves:
        moves = "\n".join(f"- `{m}`" for m in summary.example_moves)
        lines.append("### Example captured moves (roster-matched)")
        lines.append(moves)
        lines.append("")
    lines.append("### Per-company detail")
    lines.append("| Company | Roster | On-roster | Edges | Off-roster | Note |")
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
            f"| {r.edge_count} | {r.people_off_roster} | {note} |"
        )
    if summary.dry_run:
        lines.append("")
        lines.append("### Go / no-go")
        lines.append(
            "- Clean named employers on the roster + empty where the bio states "
            "no pedigree + **near-zero off-roster and self-reference** → build "
            "the persisting pipeline."
        )
        lines.append(
            "- Many off-roster names, fabricated/ambiguous employers, or heavy "
            "self-reference → tighten the prompt and re-run before the backfill."
        )
    return "\n".join(lines)
