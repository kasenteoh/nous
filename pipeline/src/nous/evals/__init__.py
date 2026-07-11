"""Golden-set eval harness for LLM prompts (W-E.1).

Two modes:

- **Offline (default, CI)** — replay committed ``recorded.json`` model
  responses through the exact runtime parse/validate/normalize path, score
  them against hand-checked ``expected.json`` ground truth, and gate the
  aggregate metrics against floors in ``tests/golden/baseline.json``.
  Deterministic, free, no network.
- **Record (opt-in, live)** — re-run every fixture input against the current
  prompt via :mod:`nous.llm.client` (requires ``DEEPSEEK_API_KEY``) and
  rewrite the ``recorded.json`` files, so a prompt edit's effect on the
  metrics becomes visible before it ships.

Entry points: the ``nous eval-prompts`` CLI command and the
``tests/test_golden_prompts.py`` pytest gate. Fixture layout and workflow are
documented in ``tests/golden/README.md``.
"""

from nous.evals.harness import (
    check_floors,
    evaluate_prompt,
    floors_from_report,
    load_baseline,
    render_report,
    save_baseline,
)
from nous.evals.prompts import PROMPT_SPECS, PromptSpec, get_spec
from nous.evals.schema import CaseSpec, PromptReport, RecordedResponse

__all__ = [
    "PROMPT_SPECS",
    "CaseSpec",
    "PromptReport",
    "PromptSpec",
    "RecordedResponse",
    "check_floors",
    "evaluate_prompt",
    "floors_from_report",
    "get_spec",
    "load_baseline",
    "render_report",
    "save_baseline",
]
