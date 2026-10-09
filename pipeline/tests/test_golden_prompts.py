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
from nous.evals.harness import iter_case_dirs, load_case_inputs, load_cases, load_recorded

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
    - inputs stay bounded (judge/funding cases a few KB; long-description
      rich cases are deliberately multi-page, ~10 KB — see the golden
      README) so prompt-building stays realistic and the repo stays light.
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


@pytest.mark.parametrize("spec", PROMPT_SPECS, ids=_SPEC_IDS)
def test_golden_prompts_build_offline(spec: PromptSpec) -> None:
    """Record mode's prompt adapters must render every fixture without the
    network — a malformed case input (a bad company_match pair JSON, a
    profile-less article_subject_match case) fails here in CI instead of in
    the paid live re-record."""
    for case_dir in iter_case_dirs(GOLDEN_DIR, spec.name):
        case_spec, input_text = load_case_inputs(case_dir)
        prompt = spec.build_prompt(case_spec, input_text)
        assert prompt.strip(), f"{case_dir.name}: empty prompt"


def test_entity_gate_prompts_render_like_the_stages() -> None:
    """The entity-gate adapters go through the stages' own input builders:
    dedup's ``_CompanyRow.to_prompt_dict`` (the latest-funding evidence line)
    and the guard's column mapping (industry_group, ``_company_hq``)."""
    from nous.evals import get_spec

    pair_spec = get_spec("company_match")
    case_spec, input_text = load_case_inputs(
        GOLDEN_DIR / "company_match" / "cases" / "bunkerhill-shared-round"
    )
    prompt = pair_spec.build_prompt(case_spec, input_text)
    assert prompt.count("- Latest funding: Series B $55,000,000 announced 2026-07-10") == 2
    assert "- HQ: Palo Alto, CA" in prompt

    guard_spec = get_spec("article_subject_match")
    case_spec, input_text = load_case_inputs(
        GOLDEN_DIR / "article_subject_match" / "cases" / "built-in-outlet-vs-built"
    )
    prompt = guard_spec.build_prompt(case_spec, input_text)
    assert "- Industry: fintech" in prompt
    assert "- HQ: Nashville, TN" in prompt
    assert f"- Headline: {case_spec.article_title}" in prompt


def test_baseline_covers_all_gated_metrics() -> None:
    """baseline.json must have a floor for every gated metric of every prompt
    (a gated metric without a floor would silently never gate)."""
    baseline = load_baseline(GOLDEN_DIR)
    for spec in PROMPT_SPECS:
        report = evaluate_prompt(spec, GOLDEN_DIR)
        floors = baseline.get(spec.name, {})
        missing = [name for name in report.gated if name not in floors]
        assert not missing, f"{spec.name}: gated metrics missing baseline floors: {missing}"
