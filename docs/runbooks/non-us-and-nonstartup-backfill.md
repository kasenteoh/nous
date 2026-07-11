# Runbook: drain non-US and non-startup rows from the live catalog

**Written:** 2026-07-10 (W-C.6 of the fable5 improvement plan)
**Status of the underlying stages:** already shipped and idempotent — this
runbook only sequences existing, gated `workflow_dispatch` levers. No code
change is involved in executing it.

## Problem

Two known leak classes are still present in rows created before the tightened
prompts/stages existed:

1. **Non-US companies** (Mistral, Clio, …) whose `hq_country` was never
   inferred — the catalog policy is US-only, so these should be soft-excluded.
2. **Non-startups the older, looser judge prompt wrongly kept** — business
   directories, coaching/course shops, agencies, decades-old businesses
   (e.g. Manta, Lucra).

Both drains are **bounded, resumable, and idempotent**: every processed row is
stamped, so re-dispatching resumes where the last run stopped and a completed
drain re-selects nothing. Neither path ever un-excludes a row.

## Lever 1 — `infer-hq-country` (non-US)

Fetches about/contact/legal pages for *shown* companies with `hq_country IS
NULL` (≤ ~8 fetches + 1 DeepSeek call per company — the most expensive
per-company stage; never on the cron) and soft-excludes non-US ones with a
recorded source.

1. **Dry-run first** (default): dispatch `pipeline.yml` with
   `run_infer_country=true`, `infer_country_limit=40`, leaving
   `infer_country_apply` **false**. Read the intended exclusions in the step
   log.

   ```sh
   gh workflow run pipeline.yml \
     -f skip_news=true -f skip_funding=true -f skip_resolve=true \
     -f skip_scrape=true -f skip_enrich=true \
     -f run_infer_country=true -f infer_country_limit=40
   ```

2. **Apply**: re-dispatch the same command with `-f infer_country_apply=true`.
   Raise `infer_country_limit` (e.g. 150) once a small applied batch looks
   right; each run stays within the step's 30-minute timeout at ≲ 40/run for
   safety, so prefer several bounded dispatches over one huge one.

3. **Repeat** until the run log reports an empty selection.

## Lever 2 — `judge-eligibility --rejudge-nonstartup-signals`

Resets the judged stamp on currently-included rows whose stored description
matches conservative non-startup prose signals, then re-judges them with the
tightened prompt (the LLM makes the final call; confirming rows are re-stamped
and never re-selected).

```sh
gh workflow run pipeline.yml \
  -f skip_news=true -f skip_funding=true -f skip_resolve=true \
  -f skip_scrape=true -f skip_enrich=true \
  -f run_rejudge_nonstartup=true -f judge_limit=200
```

Repeat with the same command until the judge step reports zero selected. The
sweep shares the normal judge step (45-min timeout); ~200/run is comfortably
inside it.

## Ordering, cost, verification

- **Order doesn't matter** — the two levers select disjoint work-lists. Running
  both in one dispatch is fine (each stage is `continue-on-error` and bounded).
- **Cost**: one DeepSeek call per swept company (~1.5k companies worst-case
  across both levers ⇒ single-digit dollars total at DeepSeek pricing) plus
  scraping time for lever 1. Flagged per the cost rule; approved as part of the
  2026-07-10 plan (W-C.6).
- **Verify**: `uv run nous db-stats` before/after (excluded-row counts), spot
  check a handful of newly excluded slugs on the live site (they must 404), and
  confirm well-known keepers (e.g. recently funded US startups) still render.
- **Rollback**: a wrongly excluded company is restored with
  `uv run nous unexclude-company <slug>` (or re-judged after a prompt fix —
  exclusions are soft, data is never deleted).
