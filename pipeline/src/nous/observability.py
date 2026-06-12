"""Pipeline run telemetry helpers.

Dependency-free: uses only stdlib + our own modules. No third-party
observability SDKs — we keep it cheap and simple.

Public API:
    emit_run_telemetry(stage)   — log ledger + optional GH step summary block
    write_step_summary(markdown) — append markdown to GITHUB_STEP_SUMMARY if set
"""

from __future__ import annotations

import logging
import os

# nous.llm.client is intentionally imported lazily inside emit_run_telemetry
# so that importing observability.py (e.g. for write_step_summary in db-stats)
# does not transitively pull in httpx / tenacity.

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
