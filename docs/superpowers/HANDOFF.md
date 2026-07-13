# Handoff — state of the world as of 2026-07-13

Written for the next agent (any model) picking this project up cold. Read
this, then root `CLAUDE.md` (conventions), then the worklog
(`docs/superpowers/fable5-worklog.md` — one entry per merged PR, the
authoritative history; **read its "Opus 4.8 pickup — 2026-07-12" section**
for the detail behind the Latest-update block below), then the two plan docs
under `docs/superpowers/plans/` (2026-07-10 improvement plan; 2026-07-11
hygiene + Wave 3). `BACKLOG.md` is annotated with what shipped.

## LATEST UPDATE — Opus 4.8 session (2026-07-12 → 07-13, ~PRs #157–#164)

Wave 3 is now genuinely LIVE and the next initiative (the SEO growth engine)
is underway. What changed since the "as of 2026-07-12" body below:

- **Frozen-prod recovery (the fire):** prod had been frozen ~a day at the
  pre-E-2 commit — every Vercel build failed because the `/companies`
  serverless function hit Vercel's 250MB limit (415MB). Root cause: Vercel's
  **Turbopack builder ignores `outputFileTracingExcludes`**. Fixed by pinning
  the web build to `next build --webpack` (#157) AND setting
  **`VERCEL_SUPPORT_LARGE_FUNCTIONS=1`** on the Vercel project — **both are now
  REQUIRED; a fresh project/clone must have the env var or deploys fail.**
  Semantic search is finally live (it had never actually deployed).
- **Perplexity / website-less-husk arc (#158–#163):** root-caused two layers —
  no `website` (resolved before the curl_cffi Cloudflare bypass PR #132) AND
  the scrape is **Cloudflare-403'd from Actions datacenter IPs** (both httpx
  and curl_cffi; a 403 short-circuits before the Playwright render). Shipped
  reusable `nous inspect-company` + `nous reresolve-company` (via `ops.yml`),
  db-stats cohort counts (**890 website-less shown companies, 882 re-drainable
  now**), and a self-bounding **re-drain of the pre-#132 cohort** (in flight
  over the crons). The structured-data describe fallback ("A") was designed +
  validated but **deferred** (marginal + an off-page `description_short`
  compliance gap).
- **Product roadmap designed** (multi-agent workflows + adversarial critique),
  owner-approved: **SEO growth engine first, drop A, market map last.** Shipped
  **migration `0036`** — the `funding_by_quarter` + `industry_funding_momentum`
  RPCs (the foundation the industry pages / `/trends` need; verified against a
  local pgvector container, full 1489-test DB suite green). **Migration head is
  now 0036.**
- **New gotcha — local DB verification:** OrbStack is installed and
  `pgvector/pgvector:pg15` is cached. For migration/RPC work, spin one up
  (`docker run -d --name nous-pg -e POSTGRES_PASSWORD=postgres -e
  POSTGRES_DB=nous_test -p 55432:5432 pgvector/pgvector:pg15`;
  `DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:55432/nous_test"`;
  `uv run alembic upgrade head`; `uv run pytest -q` runs all ~1489 DB-gated
  tests) and verify for real instead of round-tripping through CI.

## What just happened (25 merged PRs, #131–#155)

Two initiatives, both complete:

1. **2026-07-10 improvement plan** — web test suite (Vitest+RTL+Playwright),
   LLM eval golden set + harness, shared per-domain HTTP throttle, secret-leak
   prevention (gitleaks + client-bundle canary scan + `server-only`
   boundary), bug sweep (loud Vercel misconfig, one META_LEAK guard, deduped
   total-raised), prompt-version provenance stamps (migration 0031), the W-F
   description rewrite (judge/describe prompt split, ~350–600-word grounded
   profiles), discovery expansion (GeekWire/VentureBeat, uniform adapter
   hard-fail contract), slug aliases + 308 redirects (0032).
2. **Hygiene + Wave 3** — husk rescue (prominent description-less companies
   prioritized + force-rendered), canonical tag vocabulary, word-boundary
   funding keywords + GitHub-trending discovery, then the embeddings stack:
   pgvector + `embed-companies` (0033), themes (0034), semantic search
   (0035).

## Working agreement (user-set, standing)

- User owns product; agent owns technical execution, full autonomy on
  reversible engineering decisions. Stop only for product/architecture
  changes, destructive-beyond-git actions, or true blockers.
- Branch per slice (`fable5/<name>` — adopt your own prefix), PR via `gh`,
  **merge your own PR when CI is green**, squash + delete branch. Verify the
  FULL `statusCheckRollup` JSON explicitly before every merge — piping
  `gh pr checks` through grep/tail once masked a red pipeline job and main
  was red for 13 hours (worklog: "red-main incident"). Never merge red.
- Commit trailer exactly: `Co-Authored-By: Claude Opus 4.8
  <noreply@anthropic.com>` (user-specified; see worklog preamble). PR bodies
  end with the Claude Code attribution line.
- Worklog entry per merged PR; docs-only worklog commits go directly to main.
- DeepSeek is the runtime LLM — never swap it. Cost is not a constraint but
  flag any material spend before incurring it.

## Environment facts (will bite you if unknown)

- **No local Postgres/DB URL/DeepSeek key.** DB-gated tests (~500) skip
  locally and run in CI's Postgres service (`pgvector/pgvector:pg15`).
  A container runtime (OrbStack) exists — recent agents ran the full
  DB-gated suite against a local `pgvector/pgvector:pg15` container; do that
  for migration work if you can.
- **Actions is the only prod lever.** `pipeline.yml` (3-hourly; at GitHub's
  25-input cap — a new input must displace one; prefer new behavior riding
  existing steps/flags), `discovery.yml` (weekly), `backfill-discovery.yml`,
  `ops.yml` (exclude/unexclude by slug), `eval-record.yml` (live golden-set
  re-recording → pushes a branch; repo settings forbid Actions-created PRs).
- **Concurrency displacement:** DB-writing workflows share one concurrency
  group; GitHub keeps only the newest PENDING run — queued dispatches
  displace each other and the cron. Batch loops must re-dispatch on
  `cancelled` and should wait for an empty queue between dispatches or they
  starve the cron (this happened; see worklog "drain v4").
- The user's Mac disk runs near-full; prune `.claude/worktrees/` and
  node_modules/.next copies after agents finish.

## Autonomous processes currently running (no babysitting required)

- The 3-hourly pipeline cron: news/funding/scrape/enrich (+ husk rescue
  priority), `embed-companies --limit 200` (embed backlog drains ~1–2 days
  from 2026-07-12), redescribe tail, judge.
- Weekly discovery cron: VC portfolios, GitHub trending, dedup, competitors,
  `compute-themes` (TTL-gated monthly — the FIRST themes run happens on the
  next weekly run after embeddings exist).
- Both one-time prod drains are COMPLETE: non-US exclusions (runbook lever 1,
  drained to empty selection) and the W-F re-description backlog (~1.7k+
  profiles regenerated; gate = two consecutive zero-write batches).

## Verification commands

pipeline/: `uv sync && uv run ruff check . && uv run mypy src && uv run
pytest -q` (golden gate included; `uv run nous eval-prompts` for the metric
table). web/: `npm ci && npm run lint && npm run test && npm run build &&
npm run check:bundle && npm run test:e2e` (e2e structural block passes
secret-free — that's the CI contract).

## Open items, in priority order

The current initiative is the **SEO growth engine** (owner-approved order:
SEO surface first, drop "A", market map last). Build one reviewable PR at a
time; leverage parallel agents for design/critique.

1. **Industry pages** — `web/lib/industry.ts` (slug↔label from
   `listIndustryGroups()`), `/industry/[group]` + `/industry` index consuming
   the `0036` RPCs. **On-demand ISR, NOT `generateStaticParams`** (no route
   uses it; it would couple `next build` to the DB). Gate to the ~30 canonical
   `industry_group` buckets; hard-guard thin pages (sub-themes + the funding
   chart are the only net-new content vs `/companies?industry=X`).
2. **`/trends`** dashboard (funding momentum over time, hottest industries,
   biggest recent rounds) — reuses the `0036` RPCs + `ThemeFundingChart`.
3. **`/vs/[a]/[b]` compare pages** — conservative indexing: only
   competitor-edge pairs with real funding on ≥1 side; `noindex` the long
   tail. Extract a shared `CompareTable` from `/compare`.
4. **RSS feed + `/c` event timeline** — frontend-only; the timeline must
   REPLACE the existing FundingHistory/News sections on `/c/[slug]`, not
   duplicate them. RSS + on-site only (email deferred — the first cost item).
5. **Market map `/map/[industry]`** — pipeline-time PCA projection of the
   embeddings → static server SVG; land the migration early (coords fill on
   the ~monthly compute-themes cadence); keep onnx/transformers OFF the web
   function (the #157 lesson).

Verify along the way: `/themes` should populate after the weekly discovery
cron (Mon 02:00 UTC) — confirm it; the husk re-drain and `0036` auto-apply on
the next 3-hourly pipeline cron. Deferred: the structured-describe fallback
("A", with its three required fixes — see the worklog), and anchoring the
judge/funding golden floors with `--update-baseline` after a live
`eval-record` run.

## Key architecture pointers

- Enrichment: `pipeline/src/nous/pipeline/enrich_companies.py` (two-call
  judge/describe flow, stamping semantics documented inline).
- Eval harness: `pipeline/src/nous/evals/` + `pipeline/tests/golden/README.md`
  (edit prompt → re-record live → review deltas → commit).
- Embeddings: stage `embed_companies.py`; RPCs `similar_companies` (0033) and
  `semantic_companies` (0035); web query embedder `web/lib/embed-query.ts`
  (CLS pooling parity is load-bearing).
- Themes: `compute_themes.py` (KMeans, centroid slug-stability, TTL gate).
- Web data layer: `web/lib/queries.ts` (supabaseOrNull pattern: benign
  degrade off-Vercel, loud `SupabaseConfigError` on Vercel).
- Runbook for exclusion sweeps: `docs/runbooks/non-us-and-nonstartup-backfill.md`.
