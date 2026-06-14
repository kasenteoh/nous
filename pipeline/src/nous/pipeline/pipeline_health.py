"""Pipeline health check stage.

Inspects ``pipeline_runs`` to surface stages that logged ``status='empty'``
or ``status='error'`` in their most-recent run.  Designed to run as the final
step in every workflow (``if: ${{ !cancelled() }}``) so silent regressions
surface immediately in the Actions UI rather than being noticed days later.

"Latest run" definition
-----------------------
Each CLI invocation of a stage inserts exactly one ``pipeline_runs`` row
(via ``record_pipeline_run`` in ``observability.py``).  Stages do NOT share a
run-ID column, so the only natural "group this run together" key is
``started_at``.  We use **per-stage** latest: for each distinct stage name,
find the row with the greatest ``started_at``.  This is the right definition
because:

  - Stages in the two workflows (pipeline.yml / discovery.yml) run at
    different times and different cadences; there is no shared run concept.
  - A stage can be dispatched individually (workflow_dispatch); we always
    want to report on its freshest result, regardless of what other stages did.
  - The two "empty-flag" stages (``enrich-companies``, ``analyze-competitors``)
    most prone to silent failure record exactly one row per invocation with
    ``inputs_seen`` > 0 and ``rows_written == 0`` when they silently produce
    nothing.

Exit code
---------
Always exits 0 (never blocks the pipeline — stages are continue-on-error by
design).  Pass ``--strict`` to exit 1 when any stage is non-green; useful for
local checks or future PR gates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import PipelineRun
from nous.observability import write_step_summary

logger = logging.getLogger(__name__)

# Statuses that count as non-green.
_BAD_STATUSES = {"empty", "error"}


@dataclass
class StageHealth:
    """Health snapshot for a single stage's latest run."""

    stage: str
    status: str
    rows_written: int
    inputs_seen: int


@dataclass
class HealthReport:
    """Result of one pipeline-health check."""

    stages: list[StageHealth]

    @property
    def bad(self) -> list[StageHealth]:
        return [s for s in self.stages if s.status in _BAD_STATUSES]

    @property
    def all_green(self) -> bool:
        return len(self.bad) == 0


async def run_pipeline_health(session: AsyncSession) -> HealthReport:
    """Query pipeline_runs for the latest row per stage; return a HealthReport.

    Uses a subquery to find ``MAX(started_at)`` per stage, then joins back to
    retrieve the full row for that timestamp.  All via SQLAlchemy expressions
    (no raw SQL strings, per CLAUDE.md).
    """
    # Subquery: for each stage, the timestamp of its most recent run.
    latest_per_stage = (
        select(
            PipelineRun.stage,
            func.max(PipelineRun.started_at).label("latest_started_at"),
        )
        .group_by(PipelineRun.stage)
        .subquery()
    )

    # Main query: the actual rows for those (stage, started_at) pairs.
    stmt = (
        select(PipelineRun)
        .join(
            latest_per_stage,
            (PipelineRun.stage == latest_per_stage.c.stage)
            & (PipelineRun.started_at == latest_per_stage.c.latest_started_at),
        )
        .order_by(PipelineRun.stage)
    )

    result = await session.execute(stmt)
    rows: list[PipelineRun] = list(result.scalars().all())

    stages = [
        StageHealth(
            stage=row.stage,
            status=row.status,
            rows_written=row.rows_written,
            inputs_seen=row.inputs_seen,
        )
        for row in rows
    ]
    return HealthReport(stages=stages)


def emit_health_annotations(report: HealthReport) -> None:
    """Print GitHub Actions workflow commands and a step-summary table.

    Annotations (``::warning::`` / ``::error::``) surface in the Actions run
    UI immediately — even when the step exits 0.  The step summary gives a
    persistent markdown table on the run's Summary tab.

    Safe to call outside CI: annotations are harmless plain text when
    GITHUB_STEP_SUMMARY is unset; ``write_step_summary`` is a no-op then.
    """
    if report.all_green:
        print("pipeline-health: all stages green ✓", flush=True)
        _write_green_summary(report)
        return

    # Emit one annotation per bad stage.
    for stage in report.bad:
        if stage.status == "error":
            print(
                f"::error::pipeline-health: stage '{stage.stage}' logged "
                f"status=error (inputs_seen={stage.inputs_seen} "
                f"rows_written={stage.rows_written})",
                flush=True,
            )
        else:
            # status == "empty"
            print(
                f"::warning::pipeline-health: stage '{stage.stage}' logged "
                f"status=empty (inputs_seen={stage.inputs_seen} "
                f"rows_written={stage.rows_written})",
                flush=True,
            )
        logger.warning(
            "pipeline-health: stage=%s status=%s inputs_seen=%d rows_written=%d",
            stage.stage,
            stage.status,
            stage.inputs_seen,
            stage.rows_written,
        )

    _write_health_summary(report)


def _write_green_summary(report: HealthReport) -> None:
    """Append a compact all-green table to the step summary."""
    rows_md = "\n".join(
        f"| {s.stage} | {s.status} | {s.rows_written} |" for s in report.stages
    )
    md = (
        "\n### Pipeline health — all stages green ✓\n\n"
        "| stage | status | rows_written |\n"
        "| --- | --- | --- |\n"
        f"{rows_md}\n\n"
    )
    write_step_summary(md)


def _write_health_summary(report: HealthReport) -> None:
    """Append a health table highlighting non-green stages."""
    rows_md = "\n".join(
        f"| {s.stage} | **{s.status}** | {s.rows_written} |"
        if s.status in _BAD_STATUSES
        else f"| {s.stage} | {s.status} | {s.rows_written} |"
        for s in report.stages
    )
    bad_count = len(report.bad)
    md = (
        f"\n### Pipeline health — :warning: {bad_count} stage(s) non-green\n\n"
        "| stage | status | rows_written |\n"
        "| --- | --- | --- |\n"
        f"{rows_md}\n\n"
    )
    write_step_summary(md)
