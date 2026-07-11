"""Generic golden-set harness: load fixtures, replay recordings, gate on floors.

Offline mode (the default, used by CI) never touches the network: it replays
each case's committed ``recorded.json`` through the SAME parse/validate path
the runtime uses (``schema.model_validate_json``, including model
validators), scores the result against ``expected.json``, and compares the
aggregate metrics to floors committed in ``baseline.json``.

The rendered report shows metric | baseline | current | delta so a prompt
edit's effect is visible in CI logs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from nous.evals.prompts import CaseEvaluation, PromptSpec
from nous.evals.schema import CaseSpec, PromptReport, RecordedResponse

if TYPE_CHECKING:
    from collections.abc import Sequence

# Rounded-down floor granularity: floors snap DOWN to this many decimals so a
# freshly recorded baseline is never above the score that produced it.
_FLOOR_DECIMALS = 3

_BASELINE_FILENAME = "baseline.json"
_baseline_adapter: TypeAdapter[dict[str, dict[str, float]]] = TypeAdapter(
    dict[str, dict[str, float]]
)


def default_golden_dir() -> Path:
    """``pipeline/tests/golden`` resolved relative to this source tree.

    Works for the editable install `uv sync` produces. The CLI exposes
    ``--golden-dir`` for anything unusual.
    """
    return Path(__file__).resolve().parents[3] / "tests" / "golden"


def prompt_cases_dir(golden_dir: Path, prompt_name: str) -> Path:
    return golden_dir / prompt_name / "cases"


class GoldenFixtureError(Exception):
    """A fixture is malformed — a harness bug or a bad hand edit, never a
    model quality signal, so it raises instead of scoring as a miss."""


def iter_case_dirs(golden_dir: Path, prompt_name: str) -> list[Path]:
    cases_dir = prompt_cases_dir(golden_dir, prompt_name)
    if not cases_dir.is_dir():
        raise GoldenFixtureError(f"No golden cases directory at {cases_dir}")
    case_dirs = sorted(p for p in cases_dir.iterdir() if p.is_dir())
    if not case_dirs:
        raise GoldenFixtureError(f"No cases found under {cases_dir}")
    return case_dirs


def load_case_inputs(case_dir: Path) -> tuple[CaseSpec, str]:
    """Load the prompt-side fixture files (case.json + input.txt)."""
    try:
        spec = CaseSpec.model_validate_json((case_dir / "case.json").read_text())
    except (OSError, ValidationError) as exc:
        raise GoldenFixtureError(f"{case_dir.name}: bad case.json: {exc}") from exc
    try:
        input_text = (case_dir / "input.txt").read_text()
    except OSError as exc:
        raise GoldenFixtureError(f"{case_dir.name}: missing input.txt: {exc}") from exc
    if not input_text.strip():
        raise GoldenFixtureError(f"{case_dir.name}: input.txt is empty")
    return spec, input_text


def load_recorded(case_dir: Path) -> RecordedResponse:
    try:
        return RecordedResponse.model_validate_json(
            (case_dir / "recorded.json").read_text()
        )
    except (OSError, ValidationError) as exc:
        raise GoldenFixtureError(f"{case_dir.name}: bad recorded.json: {exc}") from exc


def load_cases(spec: PromptSpec, golden_dir: Path) -> list[CaseEvaluation]:
    """Load and parse every case for ``spec``.

    ``expected.json`` MUST validate against the prompt schema (it is
    hand-checked ground truth — failure is a fixture bug). ``recorded.json``
    is replayed through the same validation the runtime applies to a raw
    model response; a recording that fails validation yields
    ``recorded=None`` and is scored via the parse_rate metric.
    """
    evaluations: list[CaseEvaluation] = []
    for case_dir in iter_case_dirs(golden_dir, spec.name):
        case_spec, input_text = load_case_inputs(case_dir)
        try:
            expected = spec.schema.model_validate_json(
                (case_dir / "expected.json").read_text()
            )
        except (OSError, ValidationError) as exc:
            raise GoldenFixtureError(
                f"{case_dir.name}: expected.json invalid: {exc}"
            ) from exc

        recorded_file = load_recorded(case_dir)
        # The runtime hands the provider's raw response text to
        # schema.model_validate_json (see nous.llm.client). Recordings store
        # that JSON object; re-serializing reproduces the identical path,
        # including model validators (e.g. the implausible-roster drop).
        try:
            recorded = spec.schema.model_validate_json(
                json.dumps(recorded_file.response)
            )
        except ValidationError:
            recorded = None

        evaluations.append(
            CaseEvaluation(
                case_id=case_dir.name,
                spec=case_spec,
                input_text=input_text,
                expected=expected,
                recorded=recorded,
            )
        )
    return evaluations


def provenance_counts(spec: PromptSpec, golden_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case_dir in iter_case_dirs(golden_dir, spec.name):
        recorded = load_recorded(case_dir)
        counts[recorded.provenance] = counts.get(recorded.provenance, 0) + 1
    return counts


def evaluate_prompt(spec: PromptSpec, golden_dir: Path) -> PromptReport:
    """Run the full offline evaluation for one prompt."""
    cases = load_cases(spec, golden_dir)
    report = spec.score(cases)
    report.provenance_counts = provenance_counts(spec, golden_dir)
    return report


# ---------------------------------------------------------------------------
# Baseline floors
# ---------------------------------------------------------------------------


def load_baseline(golden_dir: Path) -> dict[str, dict[str, float]]:
    """Read committed metric floors: prompt name -> metric name -> floor."""
    path = golden_dir / _BASELINE_FILENAME
    try:
        return _baseline_adapter.validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise GoldenFixtureError(f"bad {path}: {exc}") from exc


def save_baseline(golden_dir: Path, baseline: dict[str, dict[str, float]]) -> None:
    path = golden_dir / _BASELINE_FILENAME
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")


def floors_from_report(report: PromptReport) -> dict[str, float]:
    """Derive floors from a report: each gated metric, rounded DOWN so the
    committed floor is never above the score that produced it."""
    scale = 10**_FLOOR_DECIMALS
    return {
        name: int(report.metrics[name] * scale) / scale for name in report.gated
    }


def check_floors(report: PromptReport, floors: dict[str, float]) -> list[str]:
    """Return one failure line per gated metric below its floor."""
    failures: list[str] = []
    for name in report.gated:
        floor = floors.get(name)
        if floor is None:
            failures.append(
                f"{report.prompt}: gated metric {name!r} has no baseline floor —"
                f" run `nous eval-prompts --update-baseline`"
            )
            continue
        value = report.metrics[name]
        if value < floor:
            failures.append(
                f"{report.prompt}: {name} = {value:.3f} fell below baseline floor"
                f" {floor:.3f} (delta {value - floor:+.3f})"
            )
    return failures


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

_MAX_ISSUE_LINES = 40


def render_report(
    report: PromptReport,
    floors: dict[str, float] | None,
    *,
    show_issues: bool = True,
) -> str:
    """Human-readable metrics table with per-metric delta vs baseline."""
    provenance = (
        ", ".join(f"{k}={v}" for k, v in sorted(report.provenance_counts.items()))
        or "unknown"
    )
    lines = [
        f"=== golden set: {report.prompt} "
        f"({report.case_count} cases; recordings: {provenance}) ===",
        f"{'metric':<38} {'current':>8} {'baseline':>9} {'delta':>8}  status",
        "-" * 78,
    ]
    for name, value in report.metrics.items():
        gated = name in report.gated
        floor = (floors or {}).get(name)
        if not gated:
            baseline_text, delta_text, status = "-", "-", "info"
        elif floor is None:
            baseline_text, delta_text, status = "?", "?", "NO FLOOR"
        else:
            baseline_text = f"{floor:.3f}"
            delta_text = f"{value - floor:+.3f}"
            status = "ok" if value >= floor else "FAIL"
        lines.append(
            f"{name:<38} {value:>8.3f} {baseline_text:>9} {delta_text:>8}  {status}"
        )
    if show_issues and report.issues:
        lines.append("")
        lines.append(f"case-level mismatches ({sum(len(v) for v in report.issues.values())}):")
        emitted = 0
        for case_id in sorted(report.issues):
            for message in report.issues[case_id]:
                if emitted >= _MAX_ISSUE_LINES:
                    lines.append("  ... (truncated)")
                    break
                lines.append(f"  {case_id}: {message}")
                emitted += 1
            if emitted >= _MAX_ISSUE_LINES:
                break
    return "\n".join(lines)


def render_reports(
    reports: Sequence[PromptReport], baseline: dict[str, dict[str, float]]
) -> str:
    return "\n\n".join(
        render_report(report, baseline.get(report.prompt)) for report in reports
    )
