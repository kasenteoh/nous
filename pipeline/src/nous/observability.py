"""Pipeline run telemetry helpers.

Dependency-free: uses only stdlib + our own modules. No third-party
observability SDKs — we keep it cheap and simple.

Public API:
    emit_run_telemetry(stage)   — log ledger + optional GH step summary block
    write_step_summary(markdown) — append markdown to GITHUB_STEP_SUMMARY if set
    record_pipeline_run(...)     — persist a stage run to pipeline_runs + alert
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from pydantic import BaseModel

# nous.llm.client / nous.db are intentionally imported lazily inside the
# functions that need them so that importing observability.py (e.g. for
# write_step_summary in db-stats) does not transitively pull in httpx / tenacity
# or build the DB engine.

logger = logging.getLogger(__name__)


def write_step_summary(markdown: str) -> None:
    """Append *markdown* to the GitHub Actions step summary file.

    Does nothing (silently) when GITHUB_STEP_SUMMARY is not set — safe to
    call unconditionally in both CI and local dev.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as fh:
        fh.write(markdown)


def emit_run_telemetry(stage: str) -> None:
    """Log LLM usage for *stage* and write a compact markdown block to the step summary.

    The ledger is read (not reset) so callers that run multiple stages in one
    process can still inspect the running total; reset is the caller's
    responsibility if needed.

    Logged as a single structured INFO line so it's easy to grep in CI logs:
        nous.telemetry stage=<s> calls=N prompt_tokens=N completion_tokens=N
        parse_retries=N est_cost_usd=0.0000

    nous.llm.client is imported here (not at module scope) so that importing
    observability.py for write_step_summary alone (e.g. in db-stats) does not
    transitively load httpx/tenacity.
    """
    from nous.llm.client import get_ledger

    ledger = get_ledger()
    logger.info(
        "nous.telemetry stage=%s calls=%d prompt_tokens=%d completion_tokens=%d "
        "parse_retries=%d est_cost_usd=%.4f",
        stage,
        ledger.calls,
        ledger.prompt_tokens,
        ledger.completion_tokens,
        ledger.parse_retries,
        ledger.estimated_cost_usd,
    )

    md = (
        f"\n### LLM usage — `{stage}`\n\n"
        f"| metric | value |\n"
        f"| --- | --- |\n"
        f"| calls | {ledger.calls} |\n"
        f"| prompt tokens | {ledger.prompt_tokens:,} |\n"
        f"| completion tokens | {ledger.completion_tokens:,} |\n"
        f"| parse retries | {ledger.parse_retries} |\n"
        f"| est. cost (USD) | ${ledger.estimated_cost_usd:.4f} |\n\n"
    )
    write_step_summary(md)


def _run_status(
    *, inputs_seen: int, rows_written: int, error: str | None, flag_empty: bool
) -> str:
    """Classify a pipeline run.

    'error' when the stage raised; 'empty' when ``flag_empty`` and it processed
    inputs but wrote nothing (a silent-failure signal for stages whose output
    should track their input, e.g. analyze-competitors / enrich-companies);
    else 'success'. Pure + side-effect-free so it's trivially unit-testable.
    """
    if error is not None:
        return "error"
    if flag_empty and inputs_seen > 0 and rows_written == 0:
        return "empty"
    return "success"


async def record_pipeline_run(
    stage: str,
    *,
    started_at: datetime,
    inputs_seen: int,
    rows_written: int,
    summary: BaseModel | None = None,
    flag_empty: bool = False,
    error: str | None = None,
) -> None:
    """Persist one stage execution to ``pipeline_runs`` and alert on trouble.

    Writes in its OWN session and commits it (independent of the stage's
    transaction state), so it records even when the stage rolled back. On a
    non-'success' status it prints a GitHub Actions ``::warning::`` annotation so
    the silent failure surfaces in the run UI immediately.

    Best-effort: never raises — observability must not break the pipeline.
    """
    status = _run_status(
        inputs_seen=inputs_seen,
        rows_written=rows_written,
        error=error,
        flag_empty=flag_empty,
    )

    try:
        from nous.db.models import PipelineRun
        from nous.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            session.add(
                PipelineRun(
                    stage=stage,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    status=status,
                    inputs_seen=inputs_seen,
                    rows_written=rows_written,
                    error=error,
                    summary=summary.model_dump(mode="json")
                    if summary is not None
                    else None,
                )
            )
            await session.commit()
    except Exception:
        # Recording must never sink the run; the stage's real work already ran.
        logger.exception("failed to record pipeline_run for stage %s", stage)

    if status != "success":
        detail = f" error={error}" if error else ""
        msg = (
            f"pipeline-run {stage}: status={status} "
            f"inputs_seen={inputs_seen} rows_written={rows_written}{detail}"
        )
        # A GitHub Actions annotation (surfaces in the run UI); harmless locally.
        print(f"::warning::{msg}", flush=True)
        logger.warning(msg)
