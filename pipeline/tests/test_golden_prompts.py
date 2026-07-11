"""Offline golden-set gate for LLM prompts (W-E.1).

Runs in CI on every change: replays the committed ``recorded.json`` model
responses through the runtime parse/validate/normalize path, scores them
against hand-checked ``expected.json`` ground truth, and asserts the
aggregate metrics stay at or above the floors in ``tests/golden/baseline.json``.

Deterministic and network-free — recordings are refreshed separately via
``nous eval-prompts --record`` (requires DEEPSEEK_API_KEY). See
``tests/golden/README.md`` for the full workflow.

The metrics table is printed for every run (visible with ``pytest -s`` /
``-rA``) and embedded in the assertion message on failure, so a prompt
regression shows a readable per-metric delta report in CI logs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nous.evals import (
    PROMPT_SPECS,
    PromptSpec,
    check_floors,
    evaluate_prompt,
    load_baseline,
    render_report,
)
from nous.evals.harness import iter_case_dirs, load_cases, load_recorded

GOLDEN_DIR = Path(__file__).parent / "golden"

# Keep the golden set meaningfully sized: the plan calls for ~20 hand-checked
# cases per prompt. Pruning below this floor needs a deliberate edit here.
MIN_CASES_PER_PROMPT = 15

_SPEC_IDS = [spec.name for spec in PROMPT_SPECS]


@pytest.mark.parametrize("spec", PROMPT_SPECS, ids=_SPEC_IDS)
def test_golden_metrics_meet_baseline(spec: PromptSpec) -> None:
    """Every gated aggregate metric must hold its committed baseline floor."""
    report = evaluate_prompt(spec, GOLDEN_DIR)
    floors = load_baseline(GOLDEN_DIR).get(spec.name, {})
    table = render_report(report, floors)
    print()  # keep the table left-aligned under pytest's dots
    print(table)
    failures = check_floors(report, floors)
    assert not failures, (
        "golden-set metrics regressed below baseline floors:\n"
        + "\n".join(failures)
        + "\n\n"
        + table
    )


@pytest.mark.parametrize("spec", PROMPT_SPECS, ids=_SPEC_IDS)
def test_golden_set_is_meaningfully_sized(spec: PromptSpec) -> None:
    assert len(iter_case_dirs(GOLDEN_DIR, spec.name)) >= MIN_CASES_PER_PROMPT


@pytest.mark.parametrize("spec", PROMPT_SPECS, ids=_SPEC_IDS)
def test_golden_fixtures_are_well_formed(spec: PromptSpec) -> None:
    """Structural invariants the scorers assume.

    - expected.json validates against the runtime schema (enforced inside
      load_cases, which raises GoldenFixtureError otherwise);
    - every case carries a recorded.json with a provenance stamp;
    - inputs stay small (a few KB) so prompt-building stays realistic and
      the repo stays light.
    """
    cases = load_cases(spec, GOLDEN_DIR)
    assert len(cases) >= MIN_CASES_PER_PROMPT
    for case_dir in iter_case_dirs(GOLDEN_DIR, spec.name):
        recorded = load_recorded(case_dir)
        assert recorded.provenance in ("simulated", "deepseek")
        if recorded.provenance == "deepseek":
            assert recorded.model, f"{case_dir.name}: live recording missing model id"
        input_len = len((case_dir / "input.txt").read_text())
        assert input_len <= 16_000, f"{case_dir.name}: input.txt too large ({input_len})"


def test_baseline_covers_all_gated_metrics() -> None:
    """baseline.json must have a floor for every gated metric of every prompt
    (a gated metric without a floor would silently never gate)."""
    baseline = load_baseline(GOLDEN_DIR)
    for spec in PROMPT_SPECS:
        report = evaluate_prompt(spec, GOLDEN_DIR)
        floors = baseline.get(spec.name, {})
        missing = [name for name in report.gated if name not in floors]
        assert not missing, f"{spec.name}: gated metrics missing baseline floors: {missing}"
