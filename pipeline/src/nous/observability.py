"""Pipeline run telemetry helpers.

Dependency-free: uses only stdlib + our own modules. No third-party
observability SDKs — we keep it cheap and simple.

Public API:
    emit_run_telemetry(stage)   — log ledger + optional GH step summary block
    write_step_summary(markdown) — append markdown to GITHUB_STEP_SUMMARY if set
    record_pipeline_run(...)     — persist a stage run to pipeline_runs + alert
    guard_stage(stage, main)     — run a stage; record status='error' if it raises
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import BaseModel

_T = TypeVar("_T")

# pipeline_runs.error is TEXT, but a full traceback repr can be enormous; the
# exception type + message is what the alert needs (the step log has the rest).
_MAX_ERROR_CHARS = 2000

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


def format_stage_error(exc: BaseException) -> str:
    """``"<ExcType>: <message>"``, truncated to fit a pipeline_runs.error cell."""
    text = f"{type(exc).__name__}: {exc}"
    if len(text) > _MAX_ERROR_CHARS:
        text = text[: _MAX_ERROR_CHARS - 1] + "…"
    return text


async def guard_stage(
    stage: str,
    main: Awaitable[_T],
    *,
    ignore: tuple[type[BaseException], ...] = (),
) -> _T:
    """Await *main*; if it raises, record a ``status='error'`` run, then re-raise.

    Without this, a stage that CRASHES writes no ``pipeline_runs`` row at all
    (stages record only on their success path), so ``pipeline-health
    --strict-errors`` — and the deduped GitHub-issue alert it gates — can never
    fire on the failure class it exists for. Every workflow stage step is
    ``continue-on-error``, so the red step alone is easy to miss.

    The error row must be SUPERSEDED by a later success row for the same
    ``stage`` (pipeline-health reads the latest row per stage), so only guard
    invocations whose success path records under the same stage name —
    otherwise one crash alerts forever.

    ``ignore`` lists exception types that are operator/usage errors rather than
    stage failures (the CLI passes ``click.ClickException``); they propagate
    without a row. ``BaseException`` subclasses outside ``Exception``
    (KeyboardInterrupt, SystemExit, cancellation) are never recorded.
    Recording is best-effort (``record_pipeline_run`` never raises), so the
    original exception is always the one that propagates.
    """
    started_at = datetime.now(UTC)
    try:
        return await main
    except Exception as exc:
        if not isinstance(exc, ignore):
            await record_pipeline_run(
                stage,
                started_at=started_at,
                inputs_seen=0,
                rows_written=0,
                error=format_stage_error(exc),
            )
        raise
