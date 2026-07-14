# Handoff — state of the world as of 2026-07-13

Written for the next agent (any model) picking this project up cold. Read
this, then root `CLAUDE.md` (conventions), then the worklog
(`docs/superpowers/fable5-worklog.md` — one entry per merged PR, the
authoritative history; **read its "Opus 4.8 pickup — 2026-07-12" section**
for the detail behind the Latest-update block below), then the two plan docs
under `docs/superpowers/plans/` (2026-07-10 improvement plan; 2026-07-11
hygiene + Wave 3). `BACKLOG.md` is annotated with what shipped.

## LATEST UPDATE — talent-flow feasibility gate (2026-07-13, PR #184)

ROADMAP Next **#4 (talent-flow) is feasibility-gated, not built.** Rather than
spend LLM budget blind, a $0 read-only `career-history-probe` measured whether
scraped bios carry **named** prior employers. **Prod result (2,210 companies with
pages):** 69.5% have a bio section, but named prior-employer is **only ~18% (SQL
upper bound) / ~13–15% after noise-filtering** — below the ~30% bar for a rich
graph, and many named orgs (Intel/IBM/NVIDIA) are non-catalog non-startups. So
the "Stripe → founders → companies" **graph is not well-supported by current
data**; a per-company "founder background" rider on the ~1-in-6 pages that name a
pedigree is feasible via a bounded LLM extraction (~$6.50 one-time) — a
value/cost call parked for the owner. The `career-history-probe` tool ships
(reusable to re-measure as scrape coverage grows). **Remaining Next bet that's
cleanly buildable from existing data: investor depth (#5)** — co-investment graph
from `funding_round_investors`/`company_investors`.

## LATEST UPDATE — per-entity RSS feeds shipped (2026-07-13, PR #183)

ROADMAP **Next #3 (per-entity RSS) done** — web-only, $0, works immediately (no
cadence/migration dependency, unlike the map/momentum). The global `/feed.xml`
firehose fanned out to `/c/[slug]/feed.xml`, `/industry/[group]/feed.xml`,
`/investor/[slug]/feed.xml` (route handlers, 6h ISR, `application/rss+xml`,
newest-first funding+news, canonical/slug-gated → 404, shown-cohort only). Shared
`lib/rss-items.ts` mappers (the global feed refactored onto them, byte-identical);
`<link rel="alternate">` + a visible "Follow via RSS" link on each entity page.
Built + adversarially reviewed by 2 agents (APPROVE, 0 blocking). **Remaining Next
bets: talent-flow (#4), investor depth (#5).**

## LATEST UPDATE — momentum signals shipped (2026-07-13, PRs #181/#182)

ROADMAP **Next #2 (momentum / "heating up") done** — the "open it every morning"
hook. Same 6-agent, two-workflow pattern (scout → implement → review), pipeline +
web in parallel, both adversarially reviewed (0 blocking).
- **#181 (pipeline):** `compute-momentum` — weekly `momentum_score ∈ [0,1]`
  (0.5=flat, NULL=insufficient data) as a **weight-renormalized mean over the
  PRESENT components**: news acceleration (0.50, `company_snapshots.news_count_30d`
  recent-vs-baseline), funding recency (0.35, `latest_round_date` exp-decay),
  headcount growth (0.15). Migration **0039** (`momentum_score` partial-DESC
  indexed, `momentum_computed_at`, `momentum_why` text[]). Deterministic
  (anchored to `as_of_week`), $0, weekly in `discovery.yml` after Snapshot
  companies. `--as-of-week` for backfill.
- **#182 (web):** `/trending` ("Heating up") ranked grid + `🔥 Heating up` badge
  (threshold 0.65) + pipeline-worded "why" line. Migration-order-free (empty-state
  until scores land), so independent of #181.
- **Populates:** on the weekly `discovery.yml` run once 0039 reaches prod (next
  pipeline cron applies it). **Launch reality:** `company_snapshots` is new, so
  early scores are funding-recency-dominated until ~6 weekly rows accrue per
  company (self-enriches; no code change).
- **Gotcha logged:** a parallel main-tree agent's branch got reset to main on
  origin mid-run; the work commit survived locally and was restored by
  fast-forward push. Re-verify branch tips (`git ls-remote`) after a main-tree
  agent finishes.

## LATEST UPDATE — market map shipped (2026-07-13, PRs #179/#180)

ROADMAP **Next #1 (market map) done** — the first depth feature after the Now
horizon. Built by 6 agents across two workflows (2 scout → 2 implement → 2
review), pipeline + web in parallel (isolated worktree + main tree), each
adversarially reviewed (both APPROVE, 0 blocking).
- **#179 (pipeline):** `compute-map-positions` — per-`industry_group` scikit-learn
  **PCA(2)** over description embeddings → deterministic (svd_solver="full" +
  pinned sign convention + per-axis min-max) 2D coords in three new nullable
  columns (`map_x`/`map_y`/`map_computed_at`, **migration 0038**). $0 (local CPU,
  reuses the `embeddings` uv group), per-industry TTL-gated (25d) off
  `discovery.yml` → effective monthly. `Projector` Protocol seam (tests inject a
  fake; sklearn not needed to run them).
- **#180 (web):** `/map/[industry]` — a **static server SVG** (no client
  component, **no ML on the Vercel function** — the #157 lesson, proven via build
  traces). Nodes = SVG `<a>` links, funding-sized, canonical-gated, ISR,
  a11y-complete. Queries degrade to an **empty-state** until coords exist
  (migration-ordering-for-free), so the two PRs were independent.
- **To see real maps:** coords populate on the next **`discovery.yml`** run once
  migration 0038 reaches prod (next pipeline cron applies it). Until then every
  map is the empty-state by design. To populate sooner: after 0038 is on prod,
  dispatch `discovery.yml` once (it's TTL-gated, so `compute-map-positions` runs).
- **Deferred follow-ups:** interactive client renderer (d3-force) + theme
  coloring + a global theme-level meta-graph; the per-axis-vs-shared-scale visual
  tuning call (BACKLOG).

## LATEST UPDATE — Now horizon field-normalization + report-data (2026-07-13, PRs #176/#177)

ROADMAP Now **#3 and #4 done** — the data-quality "Now" horizon is now
substantially **complete** (#1–#4 shipped; #5's internal primitive shipped).
Built by **two agents in parallel** (pipeline in an isolated worktree + web in
the main tree — disjoint dirs, no parallel node_modules to blow the near-full
disk), each adversarially reviewed before merge, merged sequentially with docs
consolidated to main after.
- **#176 (pipeline):** `hq_state` canonicalized to the 2-letter USPS code
  (`util/us_state.py` — 50 states + DC, non-US → None → untouched), applied at
  the enrich write-site + a bounded idempotent `normalize-hq-state` backfill
  (`--limit`/`--dry-run`). **Routing-safe:** the code is the only form
  `/location/[state]` resolves (route uppercases the segment), so full-name rows
  that 404 today start resolving. No migration. **Now wired into the 3h cron**
  (`normalize-hq-state --limit 500`, id'd, after normalize-taxonomy) so prod
  drains automatically then no-ops; enrichment normalizes new writes too.
- **#177 (web):** per-company "Report incorrect data" `repoIssueUrl` rider on
  `/c/[slug]`; `formatUsd` exact-dollars `title` tooltips on every individual
  funding figure; `/tag/[tag]` `noindex` when <3 companies (lockstep with the
  sitemap's ≥3 filter).

**What's next:** the Now horizon is cleared and Next #1–#3 shipped (market map
#179/#180, momentum #181/#182, per-entity RSS #183), so the frontier is the tail
of the **NEXT horizon (depth)** — **talent-flow** (#4, from `people`) and
**investor depth** (#5, co-investment networks). Smaller Now follow-ups remain:
run the
`normalize-hq-state` backfill once; wire `util.completeness` into
husk-enrichment ordering; watch the `data-quality` cron report (esp. the
website-provenance / wrong-site proxy from the husk re-mining).

## LATEST UPDATE — data-quality dashboard shipped (2026-07-13, PR #175)

ROADMAP Now **#2 done** (and #5's internal primitive). New read-only
`data-quality` stage — the completeness sibling of db-stats (size) and
pipeline-health (freshness) — emits a step-summary report over the shown cohort:
field-completeness %s, **website provenance by `website_source`** (surfaces the
#174 re-mining contribution + the wrong-site proxy), the per-company
completeness-score distribution (new pure `util.completeness`, weighted 0..1),
duplicate rate, staleness. Id-free cron step next to db-stats (no writes, no
migration). **See the report in the next 3h cron run's Actions step summary** (or
dispatch `pipeline.yml`) for the real completeness numbers — that's the instrument
panel to watch as the remaining Now items ship. Next in the queue is **#3**
(field normalization: `hq_state`, `formatUsd`; and re-enable "report incorrect
data" — highest trust-per-effort). The completeness score is internal-only;
wiring it into husk-enrichment ordering + a public trust badge is a follow-up.

## LATEST UPDATE — husk website re-mining shipped (2026-07-13, PRs #172–#174)

ROADMAP Now #1 is **done**. The `resolve-website-fallback` stage resolves
website-less husks from sources that were never the origin homepage — **Wikidata
"official website"** (P856, name + org-type + country matched) and **outbound
links in already-sourced news article bodies** (re-fetching the article, not the
Cloudflare-origin) — $0, idempotent, provenance recorded per site
(`website_source` + `website_source_url`). **Migration head is now 0037** (also
adds `website_fallback_checked_at`, the stage's own rotation stamp, separate from
resolve-homepages' `website_resolved_at`). It's **live in the 3h cron** (id'd
step before resolve-homepages, `--limit 25`), so prod drains ~25 husks/run
(gradual = safe first application). A **30-husk prod dry run** resolved 37% at
~10/11 precision, 0 conflicts (via `resolve-website-fallback.yml`, the dispatch
lever — dry-run default, also a faster-backfill knob).

Gotchas learned this session:
- **`workflow_dispatch` must be on the default branch to be triggerable**, and a
  migration whose file is absent from the branch the cron runs would crash its
  `alembic upgrade head`. Those two together forced a **3-PR split** (dispatch
  workflow #172 → schema/migration #173 → stage #174) to run a real *pre-merge*
  prod dry run. Keep that ordering for any future stage that needs a pre-merge
  prod measurement + a new migration.
- **`news_articles.raw_content` / `raw_pages.content` store visible TEXT, not
  HTML** — no `<a href>` survives, so link-mining re-fetches the article live.
  And `raw_pages` is company-scoped (not VC-portfolio pages); the portfolio
  adapters already capture `entry.website` at discovery — so a VC-portfolio
  re-mining source is redundant and wasn't built.
- **Residual precision risk:** a NULL-`hq_country` husk with a generic name can
  still match a same-named *foreign* company on Wikidata (the dry run's "Apex
  Technologies" → French "APEX Technologies" case). The country cross-check only
  fires on a *confirmed* conflict (won't drop correct foreign matches like
  Taxfix→.de). Every write is sourced + reversible; the re-enabled "report
  incorrect data" link (Now #4) is the human catch. Watch the wrong-site rate on
  the data-quality dashboard (Now #2).

## LATEST UPDATE — roadmap + data-quality pivot (2026-07-13, PR #171)

The **SEO growth engine** (the initiative in the older "Open items" list) is
now SHIPPED end-to-end on the `0036` RPC foundation (#164): industry pages
(#165), `/trends` (#166), `/vs` + shared `CompareTable` (#167, competitors-embed
fix #168), `/feed.xml` RSS (#169), and the unified `/c` event timeline (#170).
Only the **market map** (old item 5) was left un-built.

A product-strategy pass with the owner then reset direction and added a living
roadmap (#171):
- **`ROADMAP.md` (new, repo root)** — the strategic layer above `BACKLOG.md`, as
  Now / Next / Later horizons. **North star is now DATA QUALITY FIRST, then
  depth** — a deliberate pivot from pure SEO/distribution toward earning trust
  before adding surfaces.
- **"Route around, don't evade"** — the ~890 husk companies (Cloudflare-403'd
  from Actions IPs) get resolved from sources that were never the origin
  homepage (news/portfolio outbound links → Wikidata → Common Crawl). Proxy/
  account/evasion tactics are **rejected on principle** (contradict the sourcing
  moat, rot on Cloudflare updates, unnecessary since husks are prominent).
- **`CLAUDE.md`** gained a **"Keeping the docs current"** convention (doc upkeep
  is part of "done": backlog / roadmap / handoff / worklog).
- **The market map is demoted to the Next horizon;** the data-quality Now horizon
  is the priority. See the reordered "Open items" below and `BACKLOG.md`'s
  "2026-07-13 ROADMAP 'Now' horizon" section.

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
  `ops.yml` (exclude/unexclude by slug), `resolve-website-fallback.yml` (husk
  re-mining dry-run/backfill lever, dry-run default), `eval-record.yml` (live
  golden-set re-recording → pushes a branch; repo settings forbid
  Actions-created PRs).
- **Concurrency displacement:** DB-writing workflows share one concurrency
  group; GitHub keeps only the newest PENDING run — queued dispatches
  displace each other and the cron. Batch loops must re-dispatch on
  `cancelled` and should wait for an empty queue between dispatches or they
  starve the cron (this happened; see worklog "drain v4").
- The user's Mac disk runs near-full; prune `.claude/worktrees/` and
  node_modules/.next copies after agents finish.

## Autonomous processes currently running (no babysitting required)

- The 3-hourly pipeline cron: news/funding, `resolve-website-fallback --limit 25`
  (husk re-mining, NEW #174 — drains ~25 website-less husks/run before
  resolve-homepages), scrape/enrich (+ husk rescue priority),
  `embed-companies --limit 200` (embed backlog drains ~1–2 days from
  2026-07-12), redescribe tail, judge, then the read-only reports (db-stats,
  `data-quality` NEW #175, pipeline-health) → Actions step summary.
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

The **ROADMAP "Now" horizon — data quality** is now substantially **COMPLETE**
(#1–#4 shipped; #5's internal primitive shipped). Remaining Now follow-ups are
small (below). The frontier is now the **NEXT horizon (depth)** — see `ROADMAP.md`.

1. ~~**Husk website re-mining**~~ — **SHIPPED (#172/#173/#174).** Live in the cron; drains ~25/run.
2. ~~**Data-quality dashboard**~~ — **SHIPPED (#175).** Read-only `data-quality` cron report.
3. ~~**Field normalization**~~ — **SHIPPED (#176/#177).** `hq_state`→USPS code (+ `normalize-hq-state` backfill), `formatUsd` exact-$ tooltips, thin-tag `noindex`.
4. ~~**Re-enable "report incorrect data"**~~ — **SHIPPED (#177).** Per-company `repoIssueUrl` rider on `/c/[slug]`.
5. ~~**Per-company completeness score**~~ — **internal primitive SHIPPED (#175).**

**Small Now follow-ups (do opportunistically):**
- Wire `util.completeness` into husk-enrichment prioritisation ordering; fold in
  `extraction_confidence`; expose a public trust badge (Later — provenance UI).
- Watch the `data-quality` cron report — esp. the website-provenance breakdown /
  wrong-site proxy for the husk re-mining (the Apex-class residual).

The frontier is now the **NEXT horizon (depth)**, detailed just below.

**NEXT horizon (depth, after the foundation):** the **market map** (#179/#180),
**momentum signals** (#181/#182), and **per-entity RSS** (#183) SHIPPED (see the
top update blocks). Remaining Next bets: **talent-flow** from `people` (founder
previously-at, repeat founders, exec moves) and **investor depth** (co-investment
networks, portfolio momentum). Full detail in `ROADMAP.md`.

Deferred (unchanged): the structured-describe fallback ("A", with its three
required fixes — see the worklog), and anchoring the judge/funding golden
floors with `--update-baseline` after a live `eval-record` run.

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
