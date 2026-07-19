# Golden-set prompt evals

Hand-checked fixtures + a two-mode harness that stops prompt edits from
shipping blind. Every prompt change should show its precision/recall deltas
here before it merges.

Currently covered prompts (the highest-value ones; the harness is generic
— see "Adding a prompt"):

| prompt                     | cases | response schema             |
|----------------------------|-------|-----------------------------|
| `company_description`      | 20    | `CompanyDescription`        |
| `company_description_long` | 16    | `CompanyLongDescription`    |
| `funding_extraction`       | 20    | `FundingExtraction`         |
| `career_history`           | 16    | `CareerHistoryExtraction`   |
| `source_verification`      | 10    | `SourceVerification`        |
| `describe_fallback`        | 16    | `DescribeFallbackResult`    |

`career_history` (the talent-flow founder-background rider) gates
`empty_accuracy` (the empty-not-fabricate dial — most bios name no pedigree),
`people_*` / `moves_*` P/R over founder names and (person → prior company)
edges, and the grounding proxy (every prior-company name must appear verbatim
in the input). Its `case.json` carries a `roster` (the leadership allow-list the
prompt attributes against).

`describe_fallback` (the third-party-grounded description for the unscrapable
residue — the site's only GENERATIVE-from-third-party path) gates
`descriptor_grounding_min` (floor **1.0**, the no-fabrication tooth: every
PRODUCED description's echoed `grounding_descriptor` must be a normalized
substring of the evidence, checked with the runtime's own
`_descriptor_in_evidence` — one ungrounded echo drives it to 0, exactly like
`source_verification`'s `grounding_min`), plus `null_accuracy` (funding-only /
entity-ambiguous / thin evidence → null is correct) and `described_accuracy`
(a genuinely groundable description must be produced). Its `input.txt` is the
caller-assembled EVIDENCE block (Wikidata facts first, then corroborated
article title/excerpts, each with a `(source: url)` suffix) — i.e. exactly what
the stage passes to `build_prompt`. Its recordings are **simulated
PLACEHOLDERS** pending the first live re-record via `eval-record.yml` (the
DeepSeek key exists only in Actions); the `null_accuracy` / `described_accuracy`
floors are hand-set below the placeholder 1.0 (0.8) until live behavior anchors
them, while `descriptor_grounding_min` stays at 1.0 because the gate is a hard
no-fabrication invariant, not a quality dial that live recordings can lower.

Since the W-F split, `company_description` is the *judge* (classification,
people, HQ, `description_short`) and `company_description_long` is the
dedicated long-form profile written by a second enrich pass. The long set's
gated metrics: `insufficiency_accuracy` (null exactly when the input cannot
support an honest profile), `structure_pass_rate` (>= 4 paragraphs / >= 300
words on rich inputs, padding caps on thin ones), and the grounding proxy
over the profile text.

## Layout

```
tests/golden/
  baseline.json                 # committed metric floors (the CI gate)
  <prompt>/cases/<case_id>/
    input.txt                   # document text the prompt receives (cleaned
                                # page text / article body — post
                                # extract_visible_text, i.e. exactly what the
                                # runtime stage passes to build_prompt)
    case.json                   # company_name, prompt variant, reviewer notes
    expected.json               # hand-checked ground truth (must validate
                                # against the runtime schema)
    recorded.json               # a recorded model response + provenance
```

`recorded.json` carries a `provenance` field: `"simulated"` means the
response was hand-authored (see "Provenance" below), `"deepseek"` means
record mode wrote it from a live model call (with `model` + `recorded_at`).

## Offline mode (CI, default)

```
uv run pytest tests/test_golden_prompts.py          # the CI gate
uv run nous eval-prompts                             # same scoring, full table
uv run nous eval-prompts --prompt funding_extraction # one prompt only
```

No network, no API key, fully deterministic. Each `recorded.json` is replayed
through the SAME parse/validate path the runtime uses
(`schema.model_validate_json`, including model validators such as
company_description's implausible-roster drop) plus the stage's
post-validation normalization (tag normalization, industry canonicalization).
The scored metrics are then asserted against the floors in `baseline.json`,
and a `metric | current | baseline | delta` table is printed so regressions
are readable in CI logs.

Metric families:

- **Slot precision/recall/F1** for nullable extraction fields (amounts,
  dates, locations, status events). A fabricated value costs precision, a
  missed value costs recall, a wrong value costs both.
- **Exact-match accuracy** for classification-ish fields
  (`website_state`, `is_startup`, `is_funding_announcement`).
- **Set precision/recall/F1** for list fields (investors as
  (lead|other, name) pairs, people by name, tags after normalization).
- **Structural checks + grounding** for free-text descriptions: length and
  paragraph bounds, and a no-fabrication proxy — proper nouns and numbers in
  the output must appear in the input text. Grounding is a proxy, not a
  hallucination oracle; treat drops as a signal to read the diff.

Gated metrics are listed per prompt in `nous/evals/prompts.py`; informational
metrics (e.g. `confidence_accuracy`) are printed but not gated. Tag overlap
(`tags_*`) is deliberately informational: the first live re-recording showed
DeepSeek chooses a systematically different — equally reasonable — tag
vocabulary than a fixture author, so exact-set F1 measures vocabulary
agreement, not quality.

Floor discipline: `--update-baseline` snaps floors DOWN to current scores,
but several committed description-prompt floors are hand-set BELOW that
(e.g. `is_startup_accuracy` 0.85 vs a simulated 0.95) because the recordings
are still largely simulated and the first live re-record showed real DeepSeek
scoring a few points under hand-authored stand-ins. They gate catastrophes,
not noise, until floors are recalibrated on live recordings.

The long-description set's `structure_pass_rate` floor is the exception:
it is set at 0.933 (14/15 — everything except the deliberate padded-thin
failure case) because after the 2026-07-11 rich-input recalibration the
fixtures genuinely support the depth floor, and structure is exactly the
dial the W-F split exists to hold. Its remaining hand-set-below floors
(`insufficiency_accuracy` 0.85, `grounding_mean` 0.95, `grounding_min`
0.75) stay conservative until the post-merge live re-record anchors them.

## Record mode (live, opt-in, paid)

```
DEEPSEEK_API_KEY=... uv run nous eval-prompts --record
DEEPSEEK_API_KEY=... uv run nous eval-prompts --record --prompt company_description
```

Re-runs every fixture input against the *current* prompt via
`nous/llm/client.py` (the only LLM entry point), rewrites the
`recorded.json` files with `provenance: "deepseek"`, then rescores and
prints the delta table. Without the key it refuses with a clear error; the
pytest gate never calls the network at all. Cost: ~40 small prompts per full
run — fractions of a cent at current DeepSeek pricing, but it is a paid call,
so it stays opt-in and out of CI until a key is provisioned there.

## The prompt-edit workflow

1. Edit the prompt file under `src/nous/llm/prompts/`.
2. `uv run nous eval-prompts --record` (needs `DEEPSEEK_API_KEY`).
3. Read the printed metric deltas and the case-level mismatch lines. Improved
   or held? Good. Regressed? Iterate on the prompt.
4. If the new numbers are the new normal, run
   `uv run nous eval-prompts --record --update-baseline` and review the
   `baseline.json` diff — floors snap DOWN to the achieved scores, so raising
   a floor above current behavior is always a deliberate hand edit.
5. Commit the prompt edit + refreshed `recorded.json`s + `baseline.json`
   together, so reviewers see the quality delta next to the prompt diff.

Adding/curating cases: write `input.txt` + `case.json`, hand-author
`expected.json` by reading the input carefully (it must validate against the
schema — the loader enforces this), then run record mode to produce
`recorded.json`. Bias toward the hard cases the prompt files warn about
(testimonial leakage, stated-total vs per-round amounts, month-only dates,
non-USD amounts, parked domains, directories/agencies, non-US companies,
...).

Size inputs to the contract the case exercises. Judge and funding cases
stay small (a few KB). The long-description "rich" cases must be genuinely
rich — realistic multi-page site dumps of ~1,500+ words (~10 KB), because
the prompt's own never-pad rule means an honest model cannot write a
350-600-word profile from ~250 words of source. The first live re-recording
(2026-07-11) proved this the hard way: 12 of 13 structure-scored cases
failed the rich depth floor purely because the fixtures were thin, with
output length tracking input length. Thin/null cases stay deliberately
tiny — that is the side of the contract they test.

## Provenance: simulated recordings

The initial `recorded.json`s were authored by hand (no API key was available
in the environment that created them): plausible model outputs derived from
`expected.json`, including deliberate realistic imperfections (an over-eager
roundup extraction, a euro→USD unit error, a missed stated total, a
defaulted-to-the-1st date, a promoted lead investor, a fabricated stat in a
description...). That keeps the baseline floors honestly below 1.0 and
exercises every metric's failure path. They are stand-ins, not measurements
of DeepSeek: refresh them with `--record` once a `DEEPSEEK_API_KEY` is
available (the orchestrator will re-record via CI when a key lands), then
`--update-baseline` to re-anchor the floors on real model behavior.

Note: record mode stores the *validated* response re-serialized via
`model_dump(mode="json")` — the post-schema form the runtime itself acts on.
Hand-authored recordings may contain raw pre-validation payloads (e.g. case
`company_description/06_testimonial_leakage` carries an implausible roster
precisely so the replay exercises the schema validator that drops it).

## Adding a prompt

1. Add a `PromptSpec` in `src/nous/evals/prompts.py`: schema, a
   `build_prompt(case, input_text)` adapter, and a scorer built from the
   primitives in `src/nous/evals/scoring.py`.
2. Create `tests/golden/<prompt>/cases/` fixtures (~20, hard-case-heavy).
3. Run `uv run nous eval-prompts --prompt <name> --update-baseline`.
4. The pytest gate picks the new prompt up automatically from
   `PROMPT_SPECS`.
