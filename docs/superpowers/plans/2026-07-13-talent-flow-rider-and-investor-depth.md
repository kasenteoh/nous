# Plan — talent-flow "founder background" rider, then investor depth (Next #4-lite, #5)

Written 2026-07-13 to hand the last two ROADMAP "Next" bets to a fresh session.
Owner-approved direction: **build the niche talent-flow rider (accepting the new
DeepSeek cost), then pivot to investor depth.** Context that won't survive in
git (scout specs, probe run) is distilled here so it's durable.

Read first: `docs/superpowers/HANDOFF.md` (top blocks), `ROADMAP.md` Next #4/#5,
the `#184` worklog entry, root `CLAUDE.md`.

---

## Task 1 — Talent-flow "founder background" rider (evidence-gated, owner-approved)

### Why "rider", not "graph" (the #184 probe verdict)
The `$0` `career-history-probe` (#184, run against prod, **2,210 companies with
pages**) found: bio section 69.5%, but **named** prior-employer only **~18% SQL
upper bound / ~13-15% noise-adjusted**, concentrated in prominent companies, and
the named orgs skew to big non-catalog non-startups (Intel/IBM/NVIDIA/Cisco).
So a rich "Stripe → founders → companies" graph is NOT well-supported. The
approved scope is a **per-company "Founder background / notable alumni" rider**
on the ~1-in-6 pages that name a pedigree. Extraction must return **empty** for
the ~85% that don't (no fabrication — CLAUDE.md rule). Re-run the probe
(`career-history-probe.yml` dispatch) anytime to re-measure as scrape coverage grows.

### Cost (NEW DeepSeek line — owner-approved)
One call per company reusing enrich's text (~8k in + ~300 out tokens ≈ **$0.0025/company**):
- **Dry run (20 companies): ~$0.05** · **one-time full backfill (~2,600): ~$6.50** · steady-state: pennies/run.
Measured automatically by the ledger; surface it via `observability.emit_run_telemetry`.

### Step 1 — LLM extraction DRY RUN first (husk-style evidence gate)
Before the full build, confirm extraction quality on the ~18% (does the LLM cleanly
pull named prior employers vs the regex noise?).
- **New prompt** `pipeline/src/nous/llm/prompts/career_history.py`: `PROMPT_VERSION`
  (`YYYY-MM-DD.N`); input = enrich's concatenated `raw_pages.content` (truncate to
  `MAX_PROMPT_INPUT_CHARS=32_000`) + company name + the known `people` roster (name+role)
  so it attributes prior roles to known founders/execs. Output schema (Pydantic v2,
  empty-not-fabricate):
  ```python
  class PriorRole(BaseModel): company: str; role: str | None = None; start_year: int | None = None; end_year: int | None = None
  class PersonCareer(BaseModel): name: str; prior_roles: list[PriorRole] = Field(default_factory=list)
  class CareerHistoryExtraction(BaseModel): people: list[PersonCareer] = Field(default_factory=list)
  ```
  Rules mirror the hardened prompts (`hq_country.py`, `company_description.py`): only the
  company's OWN founders/execs; IGNORE advisors/investors/customers/testimonials/board;
  copy prior-company names verbatim; **empty list rather than guess**; unknown years null.
  A `model_validator` drops a `PersonCareer` whose name isn't in the supplied roster.
- **New stage** `pipeline/src/nous/pipeline/extract_career_history.py` + CLI
  `extract-career-history --limit --dry-run`, MIRRORING `resolve_website_fallback.py`
  (dry-run runs everything, writes nothing, `render_yield_table` → `write_step_summary`;
  wrap in `try/finally: emit_run_telemetry("extract-career-history")` for the $ table).
  Select shown companies with `people` + `raw_pages` content; one `complete_json` per company.
- **New workflow** `.github/workflows/extract-career-history.yml` = clone
  `resolve-website-fallback.yml` **plus** `DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}`
  in env (the paid difference). Inputs `dry_run` (default true) + `limit` (default "20").
  Same husk 3-PR ordering caveat if a migration is needed pre-merge (see below).
- Dispatch `-f dry_run=true -f limit=20`; read yield + $ from the step summary. **Gate:**
  clean named-employer extraction (low fabrication, empties where expected) → proceed.

### Step 2 — full build (if the dry run is clean)
- **Migration 0040** (hand-written, chain off head **0039**) + model `CareerMove`
  (`pipeline/src/nous/db/models.py`), table `career_moves`:
  `company_id` (FK CASCADE, indexed), `person_name`, `person_normalized_name` (indexed),
  `prior_company_name`, `prior_company_id` (FK companies.id, nullable, indexed — the
  in-catalog graph edge), `prior_role` (nullable), `start_year`/`end_year` (SmallInt nullable),
  `source_url` (provenance), `extraction_prompt_version`.
  UniqueConstraint `(company_id, person_normalized_name, prior_company_name)`.
  **Write replace-style per company** (DELETE by company_id then INSERT) — do NOT FK to
  `people.id` (people is wiped/re-inserted every enrich run; keying to company_id +
  normalized name decouples from that churn).
- Extraction stage becomes version-gated + `--limit` bounded (select where
  `extraction_prompt_version IS NULL OR < PROMPT_VERSION`, mirroring `--redescribe-outdated`).
  Wire a bounded step into `pipeline.yml` OR run the one-time backfill via the dispatch.
- **Golden-set gating** (mandatory — CLAUDE.md): register a `career_history` PromptSpec in
  `pipeline/src/nous/evals/prompts.py`; ~20 hard-case fixtures under
  `tests/golden/career_history/` (bios with NO prior role → empty; advisor/investor leakage;
  ambiguous "worked with"); floors via `eval-prompts --update-baseline`. Live re-record is
  Actions-only (`eval-record.yml`, the DeepSeek key lives only in Actions).
- **Web rider** — a "Founder background" / "Notable alumni" subsection on
  `web/app/c/[slug]/page.tsx` adjacent to `<Team>` (`web/components/Team.tsx`), read-only
  server component, omit-when-empty, source-cited. Each prior role `role @ prior_company`,
  hyperlink to `/c/[prior_slug]` when `prior_company_id` resolves. Migration-order-free
  query pattern (explicit column select → pre-migration 400 → [] → hidden). A catalog-level
  "repeat founders" index (co-membership: same `person_normalized_name` across ≥2 companies)
  is a cheap $0 add, but low-precision (no person disambiguator — flag it).

Reference exemplars: `resolve_website_fallback.py` + `resolve-website-fallback.yml`
(dry-run stage + dispatch), `enrich_companies.py` (text loader + version-gated selection),
`llm/client.py` (`complete_json` + ledger), `observability.emit_run_telemetry`,
`derive_relationships.py` + `company_relationships` (resolved-edge derivation for
`prior_company_name → prior_company_id`).

---

## Task 2 — Investor depth (ROADMAP Next #5) — $0, cleanly buildable

Turn the investor directory from a list into a lens, from EXISTING linkage (no new data,
no LLM). Scout it fresh; the shape:
- Data: `funding_round_investors` (investor↔round) + `company_investors` (investor↔company,
  with `is_lead`) + `investors`. Co-investment = investors sharing rounds/companies.
- Surfaces (pick MVP): **co-investment network** (which investors co-invest, weighted by
  shared rounds); **"who's leading rounds in X right now"** (recent rounds by industry_group
  + their lead investors); **portfolio momentum** (aggregate the new `momentum_score` across
  an investor's portfolio — reuses #181). Likely a $0 derive-style pipeline aggregation +
  an investor-page section / a new surface, mirroring the market-map/relationships pattern.
- The per-entity investor RSS feed already exists (#183) — the depth view complements it.

---

## Working method that worked this session (13 PRs, #172-#184)
Scout (parallel read-only agents → code-grounded specs) → parallel implement in isolated
worktrees (pipeline, uv) + main tree (web, npm) → adversarial code-reviewer agent per branch
→ merge sequentially, verify the FULL statusCheckRollup green, docs to main. **Gotchas:**
(1) a parallel *main-tree* agent's branch can get reset to main on origin mid-run — re-verify
`git ls-remote` the branch SHA after it finishes, restore by fast-forward push if needed.
(2) `workflow_dispatch` must be on the default branch to trigger; a migration whose file is
absent from main crashes the cron's `alembic upgrade head` — so a stage needing a pre-merge
prod run + a new migration takes the 3-PR split (dispatch workflow → schema/migration → stage).
(3) Local DB: `docker run pgvector/pgvector:pg15` on :55432, `alembic upgrade head`, full
DB suite runs locally; remove the container after (disk near-full).
