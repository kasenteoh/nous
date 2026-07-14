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

No writes, no ``pipeline_runs`` row (mirrors db-stats / pipeline-health).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Person
from nous.observability import write_step_summary
from nous.util.completeness import (
    FIELD_WEIGHTS,
    completeness_fields,
    completeness_score,
)

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

    logger.info(
        "data-quality: shown=%d mean_completeness=%.3f husks=%d dupes=%d",
        summary.shown_total,
        summary.mean_completeness,
        summary.husks,
        summary.duplicate_groups,
    )
    return summary


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
