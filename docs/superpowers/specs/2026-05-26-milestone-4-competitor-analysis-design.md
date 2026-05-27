# Milestone 4 — Competitor Analysis

**Status:** Design approved 2026-05-26. Awaiting implementation plan.

**Scope reference:** `nous-technical-spec.md` §4.6 (`competitors` table), §5.7 (stage), §6.3 (prompt), §7.3 (page section), §9 Milestone 4.

## 1. Overview

A new monthly pipeline stage `analyze-competitors` reads enriched, industry-classified companies, calls Gemini with each company's description plus a peer list of up to 50 companies in the same `industry_group`, and writes the ranked competitor set to a new `competitors` table. The existing company detail page grows a Competitors section that renders cards — linked when the competitor resolves to an indexed company, plain text otherwise.

The stage mirrors the M3 `extract_funding` pattern: Pydantic summary, hard cap per run, stop-on-rate-limit, idempotent replace-style writes.

## 2. Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Trigger / cadence | Monthly refresh | Competitive landscapes move slowly; aligns with existing monthly VC-portfolio refresh; predictable cost ceiling. |
| Scope filter | `description_long IS NOT NULL` **AND** `industry_group IS NOT NULL` | Prompt requires both. Stricter than M2 enrichment gate but the LLM call is meaningless without industry context. |
| Unmatched competitors | Store text only (`competitor_company_id = NULL`) | Simplest. Preserves the LLM's output. Auto-creating stubs would pollute the index. Fuzzy matching can be added later if linkage rate is low. |
| Re-run semantics | Replace, not version | Delete existing `competitors` rows for the company, then insert the new set in one transaction. v1 product surfaces "current competitors," not history. |
| Re-run gate | `MAX(competitors.updated_at) < now() - INTERVAL '25 days'` (or no rows) | TTL slightly less than 30 days so the monthly run isn't blocked by a partial earlier run. |
| Cost cap | `--limit 500` default per run | Gemini 2.5 Flash free tier = 1500 RPD; well within budget for a single-day monthly sweep. |
| Peer list | Up to 50 same-industry companies, exclude self, ordered by `latest_filing_date DESC` | Recency proxies relevance. Name + `description_short` only — keeps token cost predictable. |
| Cron home | Extend existing `monthly-vc-refresh.yml`, rename to `monthly-refresh.yml` | Both jobs are monthly slow-moving enrichment; one workflow is simpler. Splittable later. |
| Competitor resolution | Exact `normalized_name` lookup only | Fuzzy match deferred. The `normalized_name` column already exists from M3. |

## 3. Database

### 3.1 New table: `competitors`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | PK, `gen_random_uuid()` default |
| `company_id` | uuid | FK `companies(id)`, NOT NULL, ON DELETE CASCADE |
| `competitor_company_id` | uuid | FK `companies(id)`, nullable, ON DELETE SET NULL |
| `competitor_name` | text | NOT NULL |
| `description` | text | 1–2 sentences |
| `reasoning` | text | Why the LLM thinks they compete |
| `rank` | smallint | NOT NULL, 1 = most direct |
| `created_at` | timestamptz | NOT NULL, default `now()` |
| `updated_at` | timestamptz | NOT NULL, default `now()`, app-set on insert |

Constraints and indexes:
- `UNIQUE (company_id, rank)` — one competitor per rank slot per company.
- `INDEX (company_id)` — primary access pattern for the company page query.
- `INDEX (competitor_company_id)` — enables future reverse lookup ("who lists X as a competitor?").

### 3.2 Migration

Generated with `uv run alembic revision --autogenerate -m "add competitors table"`. Hand-review the diff before `upgrade head`. Confirm: CASCADE on `company_id`, SET NULL on `competitor_company_id`, both indexes present, unique constraint present.

## 4. LLM prompt and schema

New file: `pipeline/src/nous/llm/prompts/competitor_analysis.py`.

### 4.1 Pydantic models

```python
class Competitor(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    reasoning: str = Field(..., min_length=1)
    rank: int = Field(..., ge=1, le=6)

class CompetitorAnalysis(BaseModel):
    competitors: list[Competitor] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _unique_consecutive_ranks(self) -> "CompetitorAnalysis":
        ranks = [c.rank for c in self.competitors]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("ranks must be 1..N with no gaps or duplicates")
        return self
```

### 4.2 Prompt builder

`build_prompt(target: Target, peers: list[Peer]) -> str`:

Inputs:
- `target`: name, `description_short`, `description_long`, `industry_group`.
- `peers`: list of `(name, description_short)`, max 50, target excluded.

The prompt:
- Identifies the target and the peer list.
- Instructs the model to prefer named peers when reasonable.
- Allows well-known competitors not in the peer list.
- Forbids fictional companies.
- Instructs the model to return an empty list rather than guess if it has no high-confidence competitors.
- Caps output at 6 competitors.
- Demands ranks 1..N with no gaps.

Validated through the existing `complete_json` client — retry once on parse failure, then surface the error (existing M2 contract).

## 5. Pipeline stage

New file: `pipeline/src/nous/pipeline/analyze_competitors.py`.

### 5.1 Entry point

```python
async def run_analyze_competitors(
    session: AsyncSession,
    *,
    limit: int = 500,
    ttl_days: int = 25,
    dry_run: bool = False,
) -> AnalyzeCompetitorsSummary
```

### 5.2 Work-queue query

Eligible companies satisfy all of:
- `description_long IS NOT NULL`
- `industry_group IS NOT NULL`
- No `competitors` row for this company, **OR** `MAX(competitors.updated_at) < now() - INTERVAL ':ttl_days days'`

Ordered by oldest-analysis-first (NULL first), then `name` for stable ordering. `LIMIT :limit`.

### 5.3 Per-company loop

For each eligible company:
1. Fetch peer list: 50 rows in the same `industry_group`, excluding the target, ordered by `latest_filing_date DESC NULLS LAST`. Project only `name` and `description_short`.
2. Build prompt → `complete_json(CompetitorAnalysis, ...)`.
3. On `LLMParseError` after the built-in single retry: increment `llm_failures`, skip company, continue loop.
4. On `LLMRateLimitError`: increment `skipped_rate_limited`, **break** the loop (preserves prior successful writes, mirrors M2/M3).
5. Resolve each competitor's `competitor_company_id` via exact `normalized_name` lookup against `companies`. Unmatched stays `NULL`.
6. In a single transaction: `DELETE FROM competitors WHERE company_id = :id`, then bulk-insert the new rows with `updated_at = now()`. Skip the write entirely if `dry_run=True`.

### 5.4 Summary model

```python
class AnalyzeCompetitorsSummary(BaseModel):
    companies_analyzed: int = 0
    competitors_written: int = 0
    competitors_linked: int = 0
    competitors_unlinked: int = 0
    llm_failures: int = 0
    skipped_rate_limited: int = 0
```

## 6. CLI

Replace the stub at `pipeline/src/nous/cli.py:339`:

```
nous analyze-competitors [--limit 500] [--ttl-days 25] [--dry-run]
```

Echoes `summary.model_dump_json(indent=2)`. Same Click pattern as `extract-funding`.

## 7. Web

### 7.1 Types

Extend `web/lib/types.ts` with:

```ts
export interface CompetitorRow {
  id: string;
  company_id: string;
  competitor_company_id: string | null;
  competitor_name: string;
  description: string | null;
  reasoning: string | null;
  rank: number;
}

export interface CompetitorWithResolved extends CompetitorRow {
  resolved: { slug: string; name: string } | null;
}
```

Add `competitors: CompetitorWithResolved[]` to `CompanyDetail`.

### 7.2 Query

Extend `getCompanyBySlug` in `web/lib/queries.ts` with a fourth parallel fetch:

```ts
supabase
  .from("competitors")
  .select("*, competitor_company:companies!competitor_company_id(slug, name)")
  .eq("company_id", companyId)
  .order("rank", { ascending: true })
```

PostgREST nested-select returns the resolved company in one round-trip. Normalize array-vs-object in JS, matching the existing `funding_round_investors` handling.

### 7.3 Component

New file: `web/components/Competitors.tsx`. Server component.

Props: `{ competitors: CompetitorWithResolved[] }`.

Renders a `<section>` containing a heading and a grid of cards. Per card:
- Header line: competitor name. Wrapped in `<Link href="/c/${resolved.slug}">` when `resolved` is non-null; otherwise plain text.
- Body: 1–2 line description.
- Footer line, muted: `Why they compete: ${reasoning}`.

Empty state: if `competitors.length === 0`, the parent page omits the section entirely (unknown = hidden, matching existing convention for the funding-history table).

### 7.4 Page integration

Insert `<Competitors competitors={data.competitors} />` between the funding-history table and the sources footer in `web/app/c/[slug]/page.tsx`. Section order matches spec §7.3.

## 8. Ops / CI

- Rename `.github/workflows/monthly-vc-refresh.yml` → `monthly-refresh.yml`.
- Add a second step to the existing job: `uv run nous analyze-competitors --limit 500`.
- Keep the existing schedule (`09:00 UTC on the 1st of each month`).
- The existing `lint.yml` automatically covers new pipeline code (ruff, mypy, pytest) and the web code (build).

## 9. Tests

All in `pipeline/tests/`. Web has no automated tests today; staying consistent — Vercel preview is the gate.

### 9.1 Prompt and schema units (`test_competitor_analysis_prompt.py`)

- Builder includes all expected fields given fixture inputs.
- `CompetitorAnalysis` validator rejects: >6 competitors, duplicate ranks, ranks with gaps, ranks outside `[1, len(competitors)]`, empty `name`.
- Builder produces output under a fixed token budget for a 50-peer fixture (regression guard).

### 9.2 Stage integration (`test_analyze_competitors_stage.py`)

Postgres-backed, `complete_json` mocked.

- Skips companies missing `description_long` or `industry_group`.
- `competitor_company_id` resolves when `normalized_name` matches; stays `NULL` otherwise.
- Replace semantics: second run with different LLM output overwrites the first; no orphan rows.
- TTL gate: `updated_at` 30 days old → re-analyzed; 10 days old → skipped.
- `LLMRateLimitError` halts the loop, increments `skipped_rate_limited`, leaves prior writes intact.
- `LLMParseError` increments `llm_failures` and continues.
- `dry_run=True` runs the query and LLM call but writes nothing.

### 9.3 Model round-trip (extend `test_models.py`)

`Competitor` row insert/read with both nullable and non-null `competitor_company_id`.

## 10. Out of scope

Explicitly deferred from M4:

- Fuzzy matching for competitor resolution (M3's pg_trgm primitive is available; revisit if linkage rate is low).
- Historical competitor tracking (no `analyzed_at` audit table).
- Reverse lookup view ("who lists X as a competitor?").
- Cross-industry-group competitors (spec §6.3 restricts peer list to same `industry_group`).
- Auto-creating stub companies for unmatched competitors.
- Logos / images in competitor cards (M5 polish).
- Editorial overrides of LLM output.

## 11. Build sequence

Chunks suitable for parallel execution where dependencies allow:

1. **DB layer** — `Competitor` SQLAlchemy model + Alembic migration + model round-trip test.
2. **LLM layer** — Prompt module, Pydantic schema, unit tests.
3. **Stage** — `run_analyze_competitors` + summary model. Depends on (1) and (2).
4. **CLI wiring** — Replace the stub with a Click command. Depends on (3).
5. **Stage integration tests** — Postgres + mocked LLM. Depends on (3).
6. **Web** — Types + queries.ts extension + `Competitors` component + page integration. Depends on (1) only (DB shape).
7. **CI** — Rename workflow file + add the new step. Depends on (4).
8. **E2E smoke** — Run the stage locally against a scratch DB row, eyeball a Vercel preview.

Chunks 1, 2, and 6 (without page integration) can run in parallel from the start. Chunk 3 fans in after 1+2. Chunks 4, 5, 7 fan in after 3. Chunk 8 is the final gate.

## 12. Verification

Per `CLAUDE.md`: `ruff check`, `mypy src`, `pytest` in `pipeline/`, plus `npm run build` in `web/`. All must pass before the PR is mergeable.
