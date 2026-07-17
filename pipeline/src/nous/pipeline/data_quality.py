"""data-quality — internal completeness report over the shown catalog.

The instrument panel for the data-quality horizon (ROADMAP Now #2). A read-only,
idempotent stage — the completeness sibling of db-stats (DB *size*) and
pipeline-health (stage *freshness*) — that emits a GitHub Actions step-summary
report so every subsequent fix (husk re-mining, field normalization, the
completeness score) becomes measurable. It reports, over the *shown* cohort
(``exclusion_reason IS NULL``):

- **Field completeness** — % with website / description / funding / logo /
  people / location / industry / tags / employees.
- **Website provenance** — counts by ``website_source`` (wikidata /
  news_outbound / legacy-unattributed), so the re-mining contribution and the
  wrong-site-rate proxy are visible.
- **Completeness score** — the per-company weighted score (util.completeness)
  aggregated: mean + a bucket histogram + husk / fully-complete counts.
- **Duplicate rate** — companies sharing a ``normalized_name`` (dedup residue).
- **Staleness** — ``last_enriched_at`` age buckets.
- **Source verification** — ``fact_verifications`` verdict counts, with the
  ``unsupported`` facts itemized: a rendered figure its cited source contradicts
  or doesn't state (the #199 internal data-quality signal — never a public
  badge; investigate the extraction, not the badge).

No writes, no ``pipeline_runs`` row (mirrors db-stats / pipeline-health).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, FactVerification, FundingRound, Person
from nous.observability import write_step_summary
from nous.pipeline.repair_duplicate_rounds import (
    NEAR_AMOUNT_TOLERANCE,
    _amounts_near,
    _dates_compatible,
    _normalized_type,
)
from nous.util.completeness import (
    FIELD_WEIGHTS,
    completeness_fields,
    completeness_score,
)
from nous.util.url import hostname

logger = logging.getLogger(__name__)

# A resolved website with no recorded source — the legacy cohort (resolve-
# homepages TLD guesses / discovery), as opposed to the attributed
# resolve-website-fallback sources.
_LEGACY_SOURCE = "unattributed"

# Score-bucket edges for the completeness histogram (0..1).
_SCORE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("husk (0.0–0.25)", 0.0, 0.25),
    ("thin (0.25–0.5)", 0.25, 0.5),
    ("partial (0.5–0.75)", 0.5, 0.75),
    ("rich (0.75–1.0)", 0.75, 1.0001),  # inclusive upper so 1.0 lands here
)


class FieldCompleteness(BaseModel):
    """Present-count and percentage for one scored field, over the shown cohort."""

    field: str
    present: int
    total: int
    pct: float


class ScoreBucket(BaseModel):
    label: str
    count: int


class UnsupportedFact(BaseModel):
    """One `unsupported` verification — a fact its cited source contradicts."""

    slug: str
    fact_kind: str
    claim: str
    source_host: str


# Itemized `unsupported` rows shown in the report (the counts above the table are
# never capped — a truncated list says "+N more" so nothing hides silently).
UNSUPPORTED_DETAIL_LIMIT: int = 25

# ── Suspect duplicate rounds (the P0 aggregation-without-dedup probe) ─────────
# The census below measures — read-only, $0 — what a near-duplicate merge gate
# WOULD touch, using the same compatibility rules repair-duplicate-rounds
# clusters with, so the gate's blast radius is known before any destructive
# merge ships (2026-07-16 QA: terrafirma $115M+$100M Series A double-count;
# sambanova Series F reported as D/E/?/F; blue-origin 10 empty shells).

# NEAR_AMOUNT_TOLERANCE / NEAR_DATE_WINDOW_DAYS now live in
# repair_duplicate_rounds (imported above) — the census and the merge passes
# always measure with the same rules.
# Itemized example rows in the report table.
SUSPECT_DETAIL_LIMIT: int = 15


class SuspectDuplicateExample(BaseModel):
    """One company with suspect duplicate funding rounds, for the report table."""

    slug: str
    kind: str  # "near_amount" | "type_conflict" | "empty_shell"
    detail: str


class SuspectDuplicateRounds(BaseModel):
    """Counts of round rows a near-duplicate merge gate would consider.

    - ``empty_shell_rows``: rows with no type/date/amount/valuation signal
      (repair-duplicate-rounds Pass 1 deletes these; counted here so the
      report shows the backlog between repair runs).
    - ``exact_dup_loser_rows``: rows the existing exact-amount collapse
      (Pass 2) would merge away.
    - ``near_amount_pairs``: same-company pairs with compatible types,
      compatible dates, and non-equal amounts within NEAR_AMOUNT_TOLERANCE —
      what the repair's Pass 2b merges. Nonzero here = backlog between runs.
    - ``type_conflict_groups``: same-company same-amount groups whose rows
      carry 2+ CONTRADICTING non-null types with at most one dated row — the
      "outlets disagree on the series letter" class (sambanova D/E/F, $1B).
      Pass 2c folds the evidence-backed subset; a PERSISTENTLY nonzero count
      is the no-stored-evidence residue worth a manual look.
    """

    empty_shell_rows: int = 0
    exact_dup_loser_rows: int = 0
    near_amount_pairs: int = 0
    type_conflict_groups: int = 0
    companies_affected: int = 0
    examples: list[SuspectDuplicateExample] = Field(default_factory=list)


class DataQualitySummary(BaseModel):
    """One data-quality report."""

    shown_total: int = 0
    fields: list[FieldCompleteness] = Field(default_factory=list)
    website_source_counts: dict[str, int] = Field(default_factory=dict)
    mean_completeness: float = 0.0
    score_buckets: list[ScoreBucket] = Field(default_factory=list)
    husks: int = 0  # score < 0.25
    fully_complete: int = 0  # score == 1.0
    duplicate_groups: int = 0  # normalized_names shared by ≥2 shown companies
    companies_in_duplicates: int = 0
    staleness: dict[str, int] = Field(default_factory=dict)
    # fact_verifications verdict → count (all rows; empty dict = none recorded).
    verification_counts: dict[str, int] = Field(default_factory=dict)
    unsupported_facts: list[UnsupportedFact] = Field(default_factory=list)
    suspect_duplicate_rounds: SuspectDuplicateRounds = Field(
        default_factory=SuspectDuplicateRounds
    )


def _pct(present: int, total: int) -> float:
    return round(present / total * 100, 1) if total else 0.0


async def run_data_quality(session: AsyncSession) -> DataQualitySummary:
    """Compute the completeness report over the shown catalog (read-only)."""
    # Company ids that have ≥1 person (one small query, checked by membership).
    people_ids = set(
        (await session.execute(select(Person.company_id).distinct())).scalars().all()
    )

    # Raw presence-driving columns for the shown cohort, in one round-trip. Core
    # columns only (not full ORM rows) so a few-thousand-row scan stays cheap; the
    # presence booleans are derived in Python via completeness_fields() — the same
    # mapping the stored compute-completeness column uses, so the two never drift.
    stmt = select(
        Company.id,
        Company.website,
        Company.description_short,
        Company.funding_round_count,
        Company.hq_country,
        Company.hq_city,
        Company.industry_group,
        Company.logo_url,
        Company.tags,
        Company.employee_count_min,
        Company.employee_count_max,
        Company.website_source,
        Company.normalized_name,
        Company.last_enriched_at,
    ).where(Company.exclusion_reason.is_(None))

    rows = (await session.execute(stmt)).all()
    summary = DataQualitySummary(shown_total=len(rows))
    if not rows:
        return summary

    field_present: Counter[str] = Counter()
    website_source_counts: Counter[str] = Counter()
    name_counts: Counter[str] = Counter()
    scores: list[float] = []
    bucket_counts: Counter[str] = Counter()
    staleness: Counter[str] = Counter()
    now = datetime.now(tz=UTC)

    for row in rows:
        fields = completeness_fields(
            website=row.website,
            description_short=row.description_short,
            funding_round_count=row.funding_round_count,
            hq_country=row.hq_country,
            hq_city=row.hq_city,
            industry_group=row.industry_group,
            has_people=row.id in people_ids,
            logo_url=row.logo_url,
            tags=row.tags,
            employee_count_min=row.employee_count_min,
            employee_count_max=row.employee_count_max,
        )
        present = fields.model_dump()
        for key, is_present in present.items():
            if is_present:
                field_present[key] += 1

        score = completeness_score(fields)
        scores.append(score)
        if score < 0.25:
            summary.husks += 1
        if score >= 1.0:
            summary.fully_complete += 1
        for label, lo, hi in _SCORE_BUCKETS:
            if lo <= score < hi:
                bucket_counts[label] += 1
                break

        if row.website is not None:
            website_source_counts[row.website_source or _LEGACY_SOURCE] += 1

        name_counts[row.normalized_name] += 1
        staleness[_staleness_bucket(row.last_enriched_at, now)] += 1

    total = summary.shown_total
    summary.fields = [
        FieldCompleteness(
            field=key,
            present=field_present[key],
            total=total,
            pct=_pct(field_present[key], total),
        )
        for key in FIELD_WEIGHTS  # stable, weight-ordered
    ]
    summary.website_source_counts = dict(website_source_counts)
    summary.mean_completeness = round(sum(scores) / len(scores), 4)
    summary.score_buckets = [
        ScoreBucket(label=label, count=bucket_counts[label])
        for label, _, _ in _SCORE_BUCKETS
    ]
    dup_names = {name for name, c in name_counts.items() if c > 1}
    summary.duplicate_groups = len(dup_names)
    summary.companies_in_duplicates = sum(name_counts[n] for n in dup_names)
    summary.staleness = dict(staleness)

    # Source-verification verdicts (all fact_verifications rows — a verification
    # outlives its company's cohort membership; it's an internal signal either
    # way). `unsupported` rows are itemized (slug + the checked claim + source
    # host) so a contradicted figure is investigable straight from the report.
    verdict_rows = (
        await session.execute(
            select(FactVerification.verdict, func.count()).group_by(
                FactVerification.verdict
            )
        )
    ).all()
    summary.verification_counts = {verdict: int(n) for verdict, n in verdict_rows}
    unsupported_rows = (
        await session.execute(
            select(
                Company.slug,
                FactVerification.fact_kind,
                FactVerification.claim,
                FactVerification.source_url,
            )
            .join(Company, Company.id == FactVerification.company_id)
            .where(FactVerification.verdict == "unsupported")
            .order_by(Company.slug, FactVerification.fact_kind)
            .limit(UNSUPPORTED_DETAIL_LIMIT)
        )
    ).all()
    summary.unsupported_facts = [
        UnsupportedFact(
            slug=slug,
            fact_kind=fact_kind,
            claim=claim,
            source_host=hostname(source_url) or source_url,
        )
        for slug, fact_kind, claim, source_url in unsupported_rows
    ]

    summary.suspect_duplicate_rounds = await _suspect_duplicate_rounds(session)

    logger.info(
        "data-quality: shown=%d mean_completeness=%.3f husks=%d dupes=%d "
        "unsupported_verifications=%d suspect_round_dups="
        "shells:%d/exact:%d/near:%d/type_conflict:%d",
        summary.shown_total,
        summary.mean_completeness,
        summary.husks,
        summary.duplicate_groups,
        summary.verification_counts.get("unsupported", 0),
        summary.suspect_duplicate_rounds.empty_shell_rows,
        summary.suspect_duplicate_rounds.exact_dup_loser_rows,
        summary.suspect_duplicate_rounds.near_amount_pairs,
        summary.suspect_duplicate_rounds.type_conflict_groups,
    )
    return summary


async def _suspect_duplicate_rounds(
    session: AsyncSession,
) -> SuspectDuplicateRounds:
    """The $0 near-duplicate-rounds census (see SuspectDuplicateRounds).

    Read-only. Uses the SAME type-compatibility view repair-duplicate-rounds
    clusters with (placeholder types → None), so these counts are exactly what
    the repair's existing passes plus the proposed near-amount gate would act
    on. Scans all rounds joined to their company slug in one query — the
    funding_rounds table is a few thousand rows, well within a report scan.
    """
    result = SuspectDuplicateRounds()
    rows = (
        await session.execute(
            select(
                Company.slug,
                FundingRound.round_type,
                FundingRound.amount_raised,
                FundingRound.announced_date,
                FundingRound.valuation_post_money,
                FundingRound.valuation_source,
            )
            .join(Company, Company.id == FundingRound.company_id)
            .where(Company.exclusion_reason.is_(None))
            .order_by(Company.slug)
        )
    ).all()

    by_slug: dict[str, list[tuple[str | None, Decimal | None, date | None]]] = (
        defaultdict(list)
    )
    shell_slugs: set[str] = set()
    for slug, rtype, amount, adate, valuation, val_source in rows:
        norm_type = _normalized_type(rtype)
        if (
            norm_type is None
            and adate is None
            and amount is None
            and valuation is None
            and val_source is None
        ):
            result.empty_shell_rows += 1
            result.examples.append(
                SuspectDuplicateExample(
                    slug=slug, kind="empty_shell", detail="no type/date/amount"
                )
            )
            shell_slugs.add(slug)
            continue
        by_slug[slug].append((norm_type, amount, adate))

    affected: set[str] = set()
    for slug, rounds in by_slug.items():
        touched = False

        # Exact-amount clusters (what the existing Pass 2 merges): same amount,
        # compatible types. Mirrors _cluster_amount_group's counting without
        # re-deriving survivors — losers = cluster size - 1.
        by_amount: dict[Decimal, list[tuple[str | None, date | None]]] = defaultdict(
            list
        )
        for norm_type, amount, adate in rounds:
            if amount is not None:
                by_amount[amount].append((norm_type, adate))
        for amount, group in by_amount.items():
            if len(group) < 2:
                continue
            typed: Counter[str] = Counter(t for t, _ in group if t is not None)
            untyped = sum(1 for t, _ in group if t is None)
            # Cluster sizes under the equal-or-null rule (see repair Pass 2).
            cluster_sizes: list[int] = list(typed.values())
            if untyped:
                if len(cluster_sizes) == 1:
                    cluster_sizes[0] += untyped
                else:
                    cluster_sizes.append(untyped)
            losers = sum(size - 1 for size in cluster_sizes if size >= 2)
            if losers:
                result.exact_dup_loser_rows += losers
                touched = True
                result.examples.append(
                    SuspectDuplicateExample(
                        slug=slug,
                        kind="exact_dup",
                        detail=f"{losers} dup row(s) at ${amount:,.0f}",
                    )
                )
            # Contradicting non-null types on ONE amount with ≤1 dated row —
            # the "outlets disagree on the series letter" class.
            if len(typed) >= 2:
                dated = sum(1 for _, d in group if d is not None)
                if dated <= 1:
                    result.type_conflict_groups += 1
                    touched = True
                    result.examples.append(
                        SuspectDuplicateExample(
                            slug=slug,
                            kind="type_conflict",
                            detail=(
                                f"${amount:,.0f} typed "
                                + "/".join(sorted(typed))
                            ),
                        )
                    )

        # Near-amount pairs (the proposed gate): compatible types, compatible
        # dates, non-equal amounts within tolerance. O(n²) per company over a
        # handful of rounds.
        amounted = [
            (t, a, d) for t, a, d in rounds if a is not None
        ]
        for i in range(len(amounted)):
            for j in range(i + 1, len(amounted)):
                t1, a1, d1 = amounted[i]
                t2, a2, d2 = amounted[j]
                types_ok = t1 is None or t2 is None or t1 == t2
                if not types_ok:
                    continue
                if not _dates_compatible(d1, d2):
                    continue
                if d1 is None and d2 is None:
                    # Mirrors Pass 2b's both-undated bail — the census counts
                    # exactly what the merge gate would touch.
                    continue
                if _amounts_near(a1, a2):
                    result.near_amount_pairs += 1
                    touched = True
                    result.examples.append(
                        SuspectDuplicateExample(
                            slug=slug,
                            kind="near_amount",
                            detail=f"${a1:,.0f} vs ${a2:,.0f}",
                        )
                    )
        if touched:
            affected.add(slug)

    result.companies_affected = len(affected | shell_slugs)
    # Deterministic, capped example table (counts above are never capped).
    result.examples = sorted(
        result.examples, key=lambda e: (e.slug, e.kind, e.detail)
    )[:SUSPECT_DETAIL_LIMIT]
    return result


def _staleness_bucket(last_enriched_at: datetime | None, now: datetime) -> str:
    if last_enriched_at is None:
        return "never enriched"
    age = now - last_enriched_at
    if age < timedelta(days=30):
        return "< 30d"
    if age < timedelta(days=90):
        return "30–90d"
    return "> 90d"


def emit_data_quality_summary(summary: DataQualitySummary) -> None:
    """Append the completeness report as a GitHub Actions step-summary table."""
    lines: list[str] = []
    lines.append("## Data quality — completeness report")
    lines.append("")
    lines.append(f"**Shown companies:** {summary.shown_total}  ·  "
                 f"**mean completeness:** {summary.mean_completeness:.2f}  ·  "
                 f"**husks (score <0.25):** {summary.husks}  ·  "
                 f"**fully complete:** {summary.fully_complete}")
    lines.append("")
    lines.append("### Field completeness")
    lines.append("| Field | Present | % |")
    lines.append("|---|--:|--:|")
    for f in summary.fields:
        lines.append(f"| {f.field.removeprefix('has_')} | {f.present} | {f.pct:.1f}% |")
    lines.append("")
    lines.append("### Completeness score distribution")
    lines.append("| Bucket | Companies |")
    lines.append("|---|--:|")
    for b in summary.score_buckets:
        lines.append(f"| {b.label} | {b.count} |")
    lines.append("")
    lines.append("### Website provenance (companies with a website)")
    lines.append("| Source | Count |")
    lines.append("|---|--:|")
    for src, count in sorted(
        summary.website_source_counts.items(), key=lambda kv: -kv[1]
    ):
        lines.append(f"| {src} | {count} |")
    lines.append("")
    lines.append("### Source verification (fact_verifications)")
    if not summary.verification_counts:
        lines.append(
            "_No verifications recorded yet — dispatch `verify-sources.yml` "
            "(`run_apply=true`) to populate._"
        )
    else:
        counts = summary.verification_counts
        lines.append(
            f"**supported:** {counts.get('supported', 0)}  ·  "
            f"**unsupported:** {counts.get('unsupported', 0)}  ·  "
            f"**uncertain:** {counts.get('uncertain', 0)}"
        )
        if summary.unsupported_facts:
            lines.append("")
            lines.append(
                "`unsupported` = the cited source contradicts or doesn't state "
                "the rendered claim — an extraction/data bug to investigate "
                "(never shown publicly)."
            )
            lines.append("")
            lines.append("| Company | Fact | Claim checked | Source |")
            lines.append("|---|---|---|---|")
            for u in summary.unsupported_facts:
                # Claims are pipeline-built (no pipes/newlines today), but keep
                # the table well-formed if that ever changes.
                claim = u.claim[:96].replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| {u.slug} | {u.fact_kind} | {claim} | {u.source_host} |"
                )
            overflow = (
                summary.verification_counts.get("unsupported", 0)
                - len(summary.unsupported_facts)
            )
            if overflow > 0:
                lines.append("")
                lines.append(f"_…and {overflow} more (table capped)._")
    lines.append("")
    lines.append("### Suspect duplicate funding rounds")
    sus = summary.suspect_duplicate_rounds
    lines.append(
        f"**empty shells:** {sus.empty_shell_rows}  ·  "
        f"**exact-amount dup rows:** {sus.exact_dup_loser_rows}  ·  "
        f"**near-amount pairs (±{NEAR_AMOUNT_TOLERANCE:.0%}):** "
        f"{sus.near_amount_pairs}  ·  "
        f"**type-conflict groups:** {sus.type_conflict_groups}  ·  "
        f"**companies affected:** {sus.companies_affected}"
    )
    if sus.examples:
        lines.append("")
        lines.append(
            "_All four classes are repaired by `repair-duplicate-rounds` in the "
            "3h cron (type-conflict only with stored pub-date evidence) — "
            "persistent nonzero counts here are the evidence-less residue._"
        )
        lines.append("")
        lines.append("| Company | Kind | Detail |")
        lines.append("|---|---|---|")
        for ex in sus.examples:
            lines.append(f"| {ex.slug} | {ex.kind} | {ex.detail} |")
    lines.append("")
    lines.append(
        f"**Duplicates:** {summary.duplicate_groups} shared-name groups "
        f"({summary.companies_in_duplicates} companies)"
    )
    lines.append("")
    lines.append("### Enrichment staleness")
    lines.append("| Age | Companies |")
    lines.append("|---|--:|")
    for label in ("never enriched", "< 30d", "30–90d", "> 90d"):
        if label in summary.staleness:
            lines.append(f"| {label} | {summary.staleness[label]} |")
    write_step_summary("\n".join(lines))
