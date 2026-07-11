"""Record mode: refresh golden-set recordings against live DeepSeek.

Opt-in and paid (fractions of a cent per full run at current DeepSeek
pricing — ~40 small prompts). Requires ``DEEPSEEK_API_KEY`` via
:mod:`nous.config`; refuses to run without it. Every LLM call goes through
:func:`nous.llm.client.complete_json` — the exact runtime path, including
schema validation and the one parse retry.

What gets written back to each case's ``recorded.json``:

- ``provenance: "deepseek"`` plus the model id and an ISO timestamp,
- ``response``: the validated response re-serialized with
  ``model_dump(mode="json")``. Note this is the *post-validation* form
  (defaults filled, model validators applied); the offline replay
  re-validates it through the same schema, so scoring is unchanged. A raw
  pre-validation transcript is deliberately not kept — the runtime never
  acts on anything that failed validation either.

A case whose live call fails (parse failure after retry, rate limit) keeps
its previous recording and is reported in the summary, so one bad call
cannot wipe a fixture.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from nous.config import Settings
from nous.evals.harness import iter_case_dirs, load_case_inputs
from nous.evals.prompts import PromptSpec
from nous.evals.schema import RecordedResponse
from nous.llm.client import DEFAULT_DEEPSEEK_MODEL, LLMError, complete_json

logger = logging.getLogger(__name__)


class RecordSummary(BaseModel):
    prompt: str
    cases_recorded: int = 0
    cases_failed: int = 0
    failures: list[str] = []


class MissingAPIKeyError(Exception):
    """DEEPSEEK_API_KEY is not configured; record mode cannot run."""


def require_api_key() -> None:
    if not Settings().DEEPSEEK_API_KEY:
        raise MissingAPIKeyError(
            "DEEPSEEK_API_KEY is not set — record mode makes live DeepSeek"
            " calls. Offline scoring (the default mode) needs no key."
        )


async def record_prompt(
    spec: PromptSpec, golden_dir: Path, *, model: str | None = None
) -> RecordSummary:
    """Re-run every fixture input for ``spec`` live and rewrite recordings."""
    require_api_key()
    summary = RecordSummary(prompt=spec.name)
    model_id = model or DEFAULT_DEEPSEEK_MODEL
    for case_dir in iter_case_dirs(golden_dir, spec.name):
        case_spec, input_text = load_case_inputs(case_dir)
        prompt = spec.build_prompt(case_spec, input_text)
        try:
            result = await complete_json(prompt, spec.schema, model=model)
        except LLMError as exc:
            # Keep the previous recording — one failed call must not wipe a
            # fixture. LLMRateLimitError subclasses LLMError, so a sustained
            # 429 lands here too; unlike pipeline stages there is no quota
            # loop to protect, the remaining handful of cases just try once.
            logger.warning("record %s/%s failed: %s", spec.name, case_dir.name, exc)
            summary.cases_failed += 1
            summary.failures.append(f"{case_dir.name}: {exc}")
            continue
        recorded = RecordedResponse(
            provenance="deepseek",
            model=model_id,
            recorded_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
            response=result.model_dump(mode="json"),
        )
        (case_dir / "recorded.json").write_text(
            json.dumps(recorded.model_dump(mode="json"), indent=2, sort_keys=False)
            + "\n"
        )
        summary.cases_recorded += 1
    return summary
