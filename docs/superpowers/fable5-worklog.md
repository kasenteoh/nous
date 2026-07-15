# Fable 5 worklog — 2026-07-10 improvement plan

Running log of the `fable5/*` PR series executing
[the improvement plan](plans/2026-07-10-fable5-coding-improvements.md).
One entry per merged PR, newest last. Worklog entries are committed directly
to `main` as post-merge bookkeeping (docs-only) so parallel workstream
branches never conflict on this file.

Conventions in effect for the series: CI (`lint.yml`) green before every
merge; squash-merge to match repo history; migrations hand-written;
authoring and review are separate passes (workstreams are implemented by
subagents in isolated worktrees or by the orchestrator, and reviewed by the
other party before ship). Commit trailers use `Co-Authored-By: Claude Opus
4.8 <noreply@anthropic.com>` exactly as specified in the master prompt —
noting that the executor is Claude Fable 5, so the trailer's model name is
inherited from the prompt, not a claim about which model wrote the code.

## PR #131 — W-B: secret-leak prevention (merged 2026-07-10)

- gitleaks full-history CI gate (`secrets` job in lint.yml); config extends
  default rules with **no** path allowlists — the two known false positives
  (public Segment writeKey + reCAPTCHA siteKey inside checked-in scraped-page
  fixtures) are fingerprint-suppressed in `.gitleaksignore`. Opt-in local
  pre-commit hook documented in README "Secret hygiene".
- `npm run check:bundle`: scans every client-visible build artifact
  (`.next/static/**` + prerendered `.html`/`.rsc`/`.body`) for server env
  identifier names and for canary secret *values* that CI now plants at build
  time (`SUPABASE_URL` deliberately stays unset so the secret-free smoke
  contract holds).
- `lib/db.ts` / `lib/queries.ts` now `import "server-only"` — a client-graph
  import is a build failure, not a comment. Boundary documented in
  `web/AGENTS.md`.
- `.gitignore` now covers all `.env` variants (`.env.production` etc. were
  previously uncovered).
- Verified by exercising all three failure modes (client import of db.ts →
  build fails; identifier in client component → bundle scan fails; planted
  PAT-shaped string → gitleaks flags).
- Integration note for W-A: vitest configs that import `lib/queries.ts` must
  alias `server-only` to a stub (the W-A branch was told mid-flight).

## PR #132 — W-E.3 + W-C.1: shared per-domain throttle (merged 2026-07-10)

- New `pipeline/src/nous/sources/_http.py`: `DomainThrottle` (per-domain
  asyncio locks + monotonic timestamps, process-wide `DEFAULT_THROTTLE`
  registry, injectable for tests) and `ThrottledHTTPClient` (throttled GET +
  the shared tenacity policy: retry 429/5xx/timeouts, never ConnectError).
- Fixes the verified W-C.1 bug: `HomepageClient`, `HeadlessBrowserClient`,
  and `NewsClient` each kept per-instance lock dicts, so two transports
  double-hit a host despite docstrings claiming cooperation. All three now
  share the default registry; the curl_cffi Chrome-impersonation fallback
  and the Playwright path pay the same per-domain toll.
- Behavior deliberately unified: the timestamp now stamps in a `finally`
  (failed requests count against the interval — strictly more polite).
- 9 new tests incl. the headline regression: a `HomepageClient` and a
  `HeadlessBrowserClient` hitting one host never fire closer than the min
  interval. Suite: 805 passed. Authored by a worktree subagent; reviewed by
  the orchestrator before ship.

## PR #133 — W-C.5 + W-C.6: current-state docs + backfill runbook (merged 2026-07-10)

- CLAUDE.md: migrations are hand-written (the `--autogenerate` guidance was
  stale and dangerous — autogenerate drops trigram/partial/unique indexes);
  documents the real cron cadence (3-hourly pipeline + weekly discovery) and
  scopes the free-tier rule around the standing DeepSeek exception.
- `nous-technical-spec.md`: the Form-D banner became a full current-state
  banner (discovery spine = VC portfolios + news, DeepSeek runtime, cadence,
  migration convention, pointers to README/CLAUDE.md).
- W-C.6: `pipeline.yml` gains a `run_rejudge_nonstartup` dispatch input
  wiring the existing `judge-eligibility --rejudge-nonstartup-signals` flag
  (workflow now at GitHub's 25-input cap — the next input added must
  displace one); the bounded sweep procedure for the non-US + non-startup
  drains is documented in `docs/runbooks/non-us-and-nonstartup-backfill.md`.

## PR #134 — W-C.3 (pipeline): one aggregator blocklist (merged 2026-07-10)

- `reject_hosts.AGGREGATOR_HOSTS` is now the single blocklist; the DDG copy
  (`AGGREGATOR_DOMAINS`) and `extract_funding._IMAGE_HOSTS` are gone. New
  `is_aggregator_host()` carries the one matching implementation; DDG's
  `is_aggregator()` and `is_aggregator_url()` both delegate. Strictly wider
  rejection (image/CDN hosts + duckduckgo.com everywhere); drift-guard tests
  pin every former single-list entry.

## PR #135 — W-A: web test suite (merged 2026-07-10)

- Vitest 4 + RTL 16 (jsdom) scaffolding; 130 tests across format/spotlight/
  compare-store/local-stores/queries/components/husk; chainable Supabase mock
  at the `createSupabaseServerClient` seam; `server-only` stubbed via alias.
- Playwright smoke grew structural cases (full filter querystring, /compare
  empty states, /api/export 200-CSV-or-deliberate-503 contract) + a
  data-backed browse→filter→company→compare→CSV journey behind
  SMOKE_HAS_DATA=1. CI web job now runs `npm run test` between Lint and Build.
- Zero production-source changes. Breakage drill: disabling the META_LEAK
  filter fails exactly the 3 leak-guard tests.

## PR #136 — W-E.1: LLM eval golden set + harness (merged 2026-07-10)

- `nous.evals` package + `nous eval-prompts` CLI: offline CI gate replays
  committed recordings through the runtime parse/validate/normalize path and
  scores vs hand-checked expected.json against `baseline.json` floors, with a
  per-metric delta table; live record mode (DEEPSEEK_API_KEY) refreshes
  recordings. 40 hand-written fixtures (20 per prompt) for
  company_description + funding_extraction; recordings are
  provenance:"simulated" until re-recorded live (no local key exists —
  re-record before/with W-F). Degraded-prompt drill: 4 mangled recordings
  fail the gate with a readable six-metric delta report.

## PR #137 — W-E.2: prompt_version provenance (merged 2026-07-10)

- `PROMPT_VERSION` constants (scheme `YYYY-MM-DD.N`) in the 5 persisting
  prompts; hand-written migration 0031 adds 6 nullable TEXT stamps
  (`companies` × 4 family-scoped, `funding_rounds`, `competitors`); every
  persisting write path stamps, incl. reconcile-round restamp-on-merge and
  merge-time gap-fill semantics. NULL = pre-versioning cohort. Unblocks W-F's
  targeted re-enrichment.

## PR #138 — W-C.2/C.3-web/C.4: web bug sweep (merged 2026-07-10)

- W-C.2: missing/partial Supabase env on Vercel now throws
  `SupabaseConfigError` (pages 500 loudly) instead of rendering an
  empty-catalog 404-everywhere site; off-Vercel (secret-free CI, local dev)
  keeps degrading to empty. All 23 swallow sites collapsed onto one
  `supabaseOrNull()` rethrowing helper. Deviation from plan: keyed on the
  `VERCEL` env rather than NODE_ENV/build-phase — simpler, covers build and
  runtime, zero CI changes.
- W-C.3 (web): META_LEAK regex now lives once in `lib/competitor-guards.ts`,
  used by Competitors.tsx and getAlternatives.
- W-C.4: total-raised = max(stated, sum deduped on (round_type, amount))
  lives once in `lib/funding.ts`; the OG card and compare table summed
  naively before (their selects now fetch round_type so the dedup key
  matches the company-page tile). Helion-style regression tests. 149 web
  tests total. **W-C is complete** (C.1 #132, C.5/C.6 #133, C.3-pipeline
  #134, C.2/C.3-web/C.4 #138).

## PRs #139/#140/#143 — eval-record workflow (merged 2026-07-11)

- `workflow_dispatch`-only workflow that re-records the golden set against
  live DeepSeek (the API key exists only as an Actions secret) and pushes a
  reviewable branch. #140 fixed a YAML parse bug (unindented commit-message
  lines terminated the `run: |` block — GitHub's tell is the workflow
  registering with its path as its name); #143 made PR-creation failure
  non-fatal (repo settings forbid Actions-created PRs; kept that way).
- First live run: all 40 fixtures recorded (0 failures). Gate correctly
  flagged simulated-vs-live drift — headline: tags_f1 0.265 vs 0.986 floor
  (live DeepSeek's tag vocabulary diverges from hand-authored tags).
  Recordings held on branch `eval-record/20260711-081233` until W-F's
  golden-set rewrite lands; floors get recalibrated against live output in
  one pass after that.

## PR #141 — W-E.4: slug aliases + 308 redirects (merged 2026-07-11)

- Migration 0032: `slug_aliases` (old_slug natural PK — documented exception;
  company_id FK CASCADE, indexed). `merge_companies` repoints the loser's
  aliases before the delete (chains converge: A→B then B→C leaves a→C),
  clears survivor-slug shadows, upserts the dying slug.
- Web: `getAliasTargetSlug` + `permanentRedirect` (308) on the miss path of
  /c/[slug] and /alternatives/[slug]. Deviation from plan: no middleware — a
  per-request DB hit to serve the rare dead-slug case loses to a
  miss-path-only lookup (valid slugs pay zero extra queries).

## PR #142 — ops workflow (merged 2026-07-11)

- Dispatch-gated `ops.yml`: choice-allowlisted `exclude-company` /
  `unexclude-company` against prod (only Actions holds DATABASE_URL — the
  runbook's manual + rollback levers had no execution path). First consumer:
  the Aidoc residual (Tel Aviv HQ confirmed in the infer-hq-country dry run;
  the apply run's fetch flaked and the one-shot `hq_country_checked_at`
  stamp would never re-select it).

## PR #144 — W-D: discovery expansion + adapter resilience (merged 2026-07-11)

- Shared JSON-island walker (`vc_portfolios/_json_island.py`) replaces the
  a16z / Founders Fund / Felicis triplicates.
- Uniform hard-fail contract: `AdapterStructuralError` + `ensure_entries` —
  zero-yield parses raise instead of returning `[]` (8 adapters silently
  degraded before); per-firm isolation in the callers verified; canaries
  strengthened for all 13 VC adapters + mangled-fixture structural-miss
  tests.
- New feeds riding ingest-news + auto-create: GeekWire funding tag (live: 6
  entries/30d) and VentureBeat main feed with a title+lede keyword gate (no
  funding-specific VB feed exists). `adapter-health` probes the six news
  feeds and is now actually wired into discovery.yml (annotate-only).
- Accelerator lists (Techstars/500 Global/Antler/Alchemist) documented as
  JS-only skips; GitHub-trending mapper deferred (needs an LLM pass).
- Known follow-up (task chip): the funding-keyword matcher substring-matches
  "evaluations" → "valuation".

## Prod operations log (2026-07-11)

- **Non-US drain (lever 1)**: dry-run batch 1 (40 checked → 3 intended
  exclusions, all verified correct); apply batch 1 excluded Ada (DE) + AIM
  (CY), Aidoc flaked → handled via ops.yml exclusion with the dry-run
  evidence; batch 2 (limit 80) dispatched.
- **Non-startup re-judge (lever 2)**: batch 1 (200-limit): 22 judged, 15
  excluded. Batch 2 dispatched.
- Batches repeat until each lever reports an empty selection, per the
  runbook.

## PR #145 — W-F: richer company descriptions (merged 2026-07-11)

- Judge/describe prompt split: new `company_description_long` whose entire
  job is the profile — seven source-gated dimensions, ~350–600-word /
  4–7-paragraph depth floor on rich input, grounding rules that outrank
  style (never pad, never invent, null over filler). Judge prompt keeps
  classification/people/HQ/short-description.
- Two-call enrich flow (judge 32k input; describe 48k, only for kept
  companies with ≥700 chars of text — thin sites get an honest null instead
  of filler). `--redescribe-outdated` regenerates only description_long for
  stale-stamped rows, oldest-version-first, riding the standing cron (no new
  workflow input). Subpages 3→5.
- Cost flagged: ≤2 calls/company (~$1–2/1000 realistic); full ~2.6k backlog
  re-description ≈ $4 realistic / $11 worst-case, one-time.
- Verified on prod after merge: AppsFlyer ~900 grounded words (rich site);
  Cognition an honest 3-paragraph thin-site profile that says so plainly.

## PRs #146–#149 — W-F hardening + the red-main incident (2026-07-11)

- **#146**: the 13:15/15:54 crons were killed at the 30-min job backstop
  (W-F's 25-min enrich budget no longer fit beside news/funding) — raised to
  45 min.
- **#147**: first live re-recording exposed that the golden "rich" inputs
  (~250 words each) couldn't honestly support the depth floor (live output
  tracked input length ~1:1). All 12 rich inputs expanded to ~1,500-word
  multi-page site text; grounding proxy's initialism artifact fixed (real
  fabrications still penalized).
- **#148**: live re-record against the rich inputs: `rich_word_mean` 242 →
  **480**, grounding_mean 0.970; floors anchored to live behavior via
  `--update-baseline`.
- **#149**: repaired a real W-F bug CI had been flagging: the describe
  prompt's version started at `2026-07-10.1`, colliding with the pre-split
  cohort's stamp, so `--redescribe-outdated` would have silently skipped
  every row the old prompt enriched. Bumped to `2026-07-11.1`.
- **Incident (owned by the orchestrator)**: main was red from #145's merge
  (~08:54) to #149's (~22:15) because the DB-gated
  `test_redescribe_selection_boundaries` failure was masked by
  `gh pr checks | grep | tail` pipelines swallowing exit codes — #145–#148
  merged without a verified-green pipeline job, violating the series' own
  first rule. Prod impact nil (the drain ran on NULL-stamp selection; live
  pages verified correct). Corrective practice: every merge now verifies the
  full `statusCheckRollup` JSON explicitly; no grep/tail between the check
  and the decision.

## Prod operations log (2026-07-11, continued)

- Non-US drain resumed post-verification: batch 3 (limit 100) applied 7 more
  sourced exclusions (Atlas/NO, Audiomob/GB, Beacon/GB, Behavox/GB, Bird/NL,
  Blockchain/AE, Boards/IL). Three-stage drain loop running: infer →
  re-judge → description re-enrichment (90/run), each to empty selection,
  with the 3-hourly cron as fallback drain.

# Initiative 2 — hygiene wave + Wave 3 (plan: 2026-07-11-hygiene-and-wave3-embeddings.md)

## PR #150 — H-1: prominent-husk rescue (merged 2026-07-11)

- Root cause of Perplexity-class husks: a 200–699-char dead zone (thin SPA
  shells too rich for the 200-char headless trigger, too thin for the
  700-char describe gate) + the 90-day refetch window + no needs-description
  selection tier ⇒ prominent companies re-scraped the same shell quarterly,
  forever.
- Fix (scrape stage only): shown description-less companies sort first,
  refetch on a 7-day cycle, and force the Playwright render below the
  describe threshold (imported from enrich — single source of truth). Enrich
  picks rescues up unchanged (end-to-end test).

## PR #151 — H-2: canonical tag vocabulary (merged 2026-07-11)

- `util/tags.py`: 96 canonical tags / 417 match keys; consolidates, never
  gates (unknown tags pass through). Applied at the enrich write path, the
  eval replay path, and as an idempotent `normalize-taxonomy` tags pass.
  Judge prompt tightened (3–6 established tags) → 2026-07-11.1; verified the
  bump re-selects no cohort.
- Review catch: the PR's pipeline check went red because the map folded
  `api-first`→`api` and `cloud-native`→`cloud`; a pre-existing DB-gated test
  correctly pinned those as distinct concepts. Fixed the map, not the test —
  and the explicit statusCheckRollup gate (post-incident discipline) is what
  caught it before merge this time.

## PR #152 — H-3: matcher word-boundaries + GitHub-trending discovery (merged 2026-07-11)

- Funding keywords now match on word boundaries ("evaluations" no longer
  triggers "valuation" — the live W-D false positive); all five feed
  consumers inherit; hyphenated/wrapped true positives pinned.
- GitHub-trending mapper: robots-checked (daily page only — `?since=` is
  disallowed), cheapest-first gating (known-owner skip → personal-account
  skip → DeepSeek company judgment, null-on-uncertainty), auto-create with
  `discovered_via=github_trending`, weekly discovery.yml step +
  adapter-health probe. <1¢/run.

## Prod operations log (2026-07-11/12, drains)

- Non-US lever: +500 checked across batches 4–8 (loop v3 continuing to
  empty). Re-judge lever: complete — its worklist drained; the 3–4/batch
  tail was interleaved crons' normal judge trickle, not rejudge re-selects.
- Re-description: ~670 profiles rewritten by the drain loop so far (batches
  of ~80–90 writes each) on top of cron contributions; v3 continues to the
  two-consecutive-zero stop.

## PR #153 — E-1: pgvector embeddings + similar companies (merged 2026-07-12)

- Migration 0033: `vector` extension (CI service image → pgvector/pgvector:pg15),
  `embedding vector(384)` + `embedded_at` + `embedding_text_hash`, and the
  `similar_companies` RPC (cosine, SQL-side exclusion filtering). No vector
  index at ~3k rows — revisit threshold documented and schema-pinned.
- `embed-companies` stage: fastembed bge-small-en-v1.5 (optional `embeddings`
  dependency group), SQL hash-diff selection, wired after enrich (200/run,
  $0 LLM). Model dir Actions-cached.
- Web: similar-companies replaces the heuristic `similar` edges when
  embeddings exist (heuristic fallback kept), with per-card similarity
  provenance. Verified by the subagent against a real pgvector container
  (1378 DB-gated tests + a live-model ranking smoke).

## PR #154 — E-3: themes (merged 2026-07-12)

- Migration 0034: `themes` (centroid vector, funding recent/prior/growth,
  prompt_version) + `company_themes`. `compute-themes`: per-industry KMeans
  (deterministic; HDBSCAN rejected — noise-labels small industries), DeepSeek
  cluster naming (null-over-fabricate: incoherent clusters dropped),
  replace-per-industry with ≥0.9-cosine centroid matching for slug stability
  (re-run with unchanged embeddings = zero LLM calls), 25-day TTL gate inside
  the stage riding weekly discovery.yml ⇒ monthly cadence. ≤$0.05/run.
- Web: /themes ranked by funding growth + /themes/[slug] (similarity-ordered
  members, server-rendered SVG funding-by-quarter, new entrants), sitemap
  ≥3-member threshold, Themes in nav. First real compute lands once the
  embed backlog drains.

## E-2 spike (no PR — evidence branch fable5/semantic-search-spike)

- Verdict GO: transformers.js runs the exact stored model in a Next 16 route
  handler on Vercel Hobby — cosine parity 0.9974 with fastembed vectors
  (CLS pooling is load-bearing), 2–3ms warm, ~58–92MB of the 250MB function
  budget (onnxruntime's native binary needs outputFileTracingIncludes).
  Supabase Edge rejected (gte-small ≠ bge space); Cloudflare Workers AI
  documented as plan-B (requires pooling:"cls"). Build in flight as 0035.

## Prod operations log (2026-07-12)

- Drain v3 → v4: v3's dispatch cadence was displacing pending crons (GitHub
  keeps one pending run per concurrency group), starving the scheduled
  scrape/enrich for hours — which is why the H-1 husk rescue hadn't landed on
  the live site. v4 waits for an empty queue before every dispatch.

## PR #155 — E-2: semantic search (merged 2026-07-12)

- Migration 0035 `semantic_companies` RPC; server-only transformers.js query
  embedder (exact stored model, CLS pooling, revision-pinned, 4s timeout,
  null → graceful lexical fallback); model bundled at build via a fail-soft
  prebuild script with the linux-x64 onnx binary traced explicitly.
- /companies hybrid blend: lexical first, semantic extras appended with
  honest totals + disclosure; gated to page 1, default sort, and no active
  column filters (extras under a filter would violate it). Independent
  code-review pass on the branch: zero findings.
- **Wave 3 complete** (E-1 #153, E-3 #154, E-2 #155). Semantic behavior
  activates in prod as the next pipeline crons apply migrations 0033–0035
  and drain the embed backlog (~1–2 days at 200/run × 8/day); until then
  every new surface degrades to its pre-Wave-3 behavior by construction.

## Prod operations log — drains COMPLETE (2026-07-12)

- **Non-US drain finished**: final batch selected 0 (≈770 companies checked
  across all batches; all exclusions carry quoted registered-office sources).
- **Re-description drain finished**: two consecutive zero-write batches after
  ~1.7k+ profiles regenerated under the W-F prompt (drain batches + cron).
- One-off failed run explained: the H-2 PR's gitleaks job flaked with
  "failed to scan Git repository: stderr is not empty" (action infra, not a
  leak); the next push re-ran green. Pin/retry the action if it recurs.

## Docs refresh + handoff (2026-07-12)

- README caught up to the shipped surface (semantic search, themes, similar
  companies, durable URLs, new stages/workflows, pgvector dev image,
  two-call enrich); BACKLOG annotated with SHIPPED markers; CLAUDE.md gained
  the prompt-version/golden-gate and embeddings conventions plus the
  DB-gated-tests-run-in-CI warning.
- `docs/superpowers/HANDOFF.md` written for the next agent: working
  agreement, environment gotchas, autonomous processes, open items,
  architecture pointers.

# Opus 4.8 pickup — 2026-07-12

## Wave 3 activation check + the frozen-prod incident (PR #157)

- **Finding:** the Wave 3 activation check found semantic search was NOT
  live — and the root cause was that **prod had been frozen at `56975a8`
  (pre-E-2) since E-2 merged**: every Vercel deploy from #155 onward failed
  because the `/companies` serverless function bundles the embedder's onnx
  runtime and hit **415MB > Vercel's 250MB** function limit. E-1
  (similar-companies) and E-3 (`/themes` route) were live because they
  deployed before the break. The E-2 spike's "58–92MB" was a LOCAL tracing
  estimate never validated against a real Vercel deploy — that gap was the
  whole incident. (Detected via `gh api …/commits/<sha>/status` context
  "Vercel"; build logs read through the user's Vercel dashboard.)
- **Why unfixable from the repo (proven across 8 preview builds):** Vercel's
  builder copies the whole `serverExternalPackages` dirs and **ignores
  `outputFileTracingExcludes`**. Locally a webpack build honors the excludes
  (92MB); on Vercel it's ~406–415MB regardless of glob form, bundler, build
  cache, or physically deleting the unused binaries from node_modules.
- **Fix, two parts:**
  1. **PR #157** — `next build --webpack` (Turbopack, Vercel's default,
     bundles the onnx assets into the function AND ignores
     `outputFileTracing*`; webpack honors it and, load-bearingly, keeps the
     query-embedding model in the function so semantic works at runtime) +
     depth-independent `**/…` tracing globs (Next's tracing root is the
     project dir locally, the repo root on Vercel).
  2. **`VERCEL_SUPPORT_LARGE_FUNCTIONS=1`** set in the Vercel project env
     (Production + Preview) — Vercel still ships ~406MB (excludes ignored),
     and this raises the limit. Unused platform binaries are never dlopen'd
     at runtime, so cold-start impact is modest. **This is now a required
     project setting; a fresh Vercel project must set it or deploys fail.**
- **Verified:** preview + production deploys green; semantic search live on
  `nous-umber.vercel.app` — `/companies?q=ai+for+logistics` returns 30
  results with the "includes semantic matches" disclosure (was 0 while
  frozen). similar-companies still live; main CI green (secrets/pipeline/web).
- Dead ends removed from the PR before merge: an `/api/health/embed`
  observability endpoint (route handlers aren't trimmed by
  `outputFileTracing*`, so it added its own 425MB function) and a
  build-time node_modules prune (ran on Vercel, reclaimed 283MB, but the
  function size never moved — Vercel doesn't build the function from the
  pruned tree).

## Remaining Wave 3 items

- **`/themes`** — route live but empty; first-ever compute is TTL-gated to
  the weekly discovery cron (Mondays 02:00 UTC; next: 2026-07-13). Not
  broken, just not due yet.
- **Perplexity husk** — still description-less on prod (generic fallback
  meta, zero prose paragraphs vs Anthropic's 27). The H-1 rescue target has
  no profile yet; open follow-up (honest-null thin-SPA vs rescue-not-cycled
  — needs a look, lower priority than the deploy freeze was). **Root-caused +
  partly fixed below.**

## Perplexity / website-less-husk arc (PRs #158–#163, 2026-07-12)

- **Root cause (two layers, both surprising):** Perplexity was NOT a
  thin-content husk — it renders 1095 chars via Playwright locally. (1) It had
  **no `website`**: resolved 2026-06-16, before the curl_cffi Cloudflare bypass
  (PR #132) existed, so plain httpx got a 403 on every TLD candidate → null;
  the 90-day re-resolve window wouldn't retry for months. (2) Even with a
  website set, the **prod scrape is Cloudflare-403'd from the Actions
  datacenter IP** (both httpx and curl_cffi), and a 403 short-circuits to
  "dead" before the Playwright render — so 0 pages, still a husk. Blocks the
  whole Cloudflare-heavy prominent-husk class.
- **Tools shipped:** read-only `inspect-company` (#158, later +news_titles /
  funding_rounds #162) and `reresolve-company [--set-url]` (#159), both via
  `ops.yml` dispatch (which now also passes `SEC_USER_AGENT`, #160 — a masked
  `tee` had hidden a crash as green).
- **Cohort fix (slices 1+2):** `db-stats` now counts the stuck cohort (#161) —
  **890 website-less shown companies, 163 funded, 882 re-drainable now** — and
  `resolve-homepages` re-admits the pre-#132 cohort for one self-bounding
  re-attempt with the stronger resolver (#163, keyed on the shared
  `_RESOLVER_GENERATION_SINCE = 2026-07-10`). No migration/CLI/dispatch input;
  rides the existing step, DeepSeek paced by the standing scrape/enrich caps.
- **Structured-describe (A) — validated, not yet built:** designed via a
  multi-agent workflow + adversarial critique. Verified on real data that A
  would work for Perplexity (its sourced news titles carry product descriptors
  — "AI search unicorn", "challenge Google in search", "$750M Microsoft
  tie-up"), so a source-compliant profile is groundable. Build deferred with
  three required fixes: strict `description_short` gating (it's syndicated
  off-page to meta/JSON-LD with no Sources footer), cross-company-title
  contamination handling, and a min-signal bar that requires a NON-funding
  descriptor.

## Product roadmap designed (2026-07-12) — "do all except monetization"

Two multi-agent workflows produced grounded designs + adversarial critiques
for the next-wave program; owner approved the order + key calls (RSS-only
digest, conservative `/vs` indexing). Sequenced: (1) website-less-husk fix
[DONE, above]; (2) industry pages `/industry/[group]` + `/trends` — the SEO
anchor, needs the `funding_by_quarter` momentum RPC in slice 1 (critique: the
per-industry chart would silently truncate at PostgREST's 1000-row cap without
it); (3) RSS feed + `/c` event timeline (frontend-only quick win; must REPLACE
the existing FundingHistory/News sections, not duplicate); (4) `/vs/[a]/[b]`
compare pages (conservative: index only competitor-edge pairs with real
funding on ≥1 side); (5) market map `/map/[industry]` (pipeline-time PCA
projection of embeddings → static server SVG; land the migration early since
coords fill on the ~monthly compute-themes cadence). Shared infra to build
once: the `0036` momentum RPCs, a `web/lib/industry.ts` slug↔label helper, an
extracted `CompareTable`. Design call for industry pages: on-demand ISR (NOT
`generateStaticParams`, which no route uses and which would couple `next build`
to the DB), gated to the 30 canonical `industry_group` buckets.

## PR #165 — industry landing pages (`/industry` + `/industry/[group]`) (merged 2026-07-13)

SEO growth-engine **slice 1**, built on the `0036` momentum RPCs.

- **Surface:** `/industry` hub lists the canonical `industry_group` buckets
  (≥3 companies), ranked by trailing recent funding with a 2-quarter growth
  chip. `/industry/[group]` = funding-by-quarter chart (server SVG from the
  `funding_by_quarter` RPC, so it can't truncate at PostgREST's 1000-row cap on
  the largest industries) + the industry's **sub-themes** (the net-new content
  vs the plain filtered list) + a funding-ranked company preview linking to
  `/companies?industry=X`.
- **New/changed code:** `web/lib/industry.ts` (new, pure — no `server-only`,
  import-safe anywhere): `industryToSlug` + `resolveIndustrySlug`, resolving
  only against the canonical list (the hard gate). `web/lib/funding.ts`:
  `quarterBucketsFromTotals` (windows the RPC's pre-aggregated rows into a
  gap-filled 8-quarter series — extracted a shared `quarterWindow`/
  `bucketsFromWindow` from `bucketFundingByQuarter`) + `fundingGrowth`.
  `web/lib/queries.ts`: `listCanonicalIndustries` (slug-deduped),
  `fundingByQuarter`, `industryFundingMomentum`, `listThemesByIndustry`.
  `ThemeFundingChart` reused (two user-visible strings neutralized so the copy
  is honest on both surfaces); nav + sitemap wired.
- **Design calls:** on-demand ISR, NO `generateStaticParams` (never couples
  `next build` to the DB — build confirms `/industry` static@6h,
  `/industry/[group]` dynamic); **hard thin-content guard** — a page with no
  funding chart AND no sub-themes is `noindex`'d via `generateMetadata.robots`
  (it carries nothing `/companies?industry=X` doesn't); **one company-count
  source** (`listCompanies.total`) so the header, the "See all N" link, and its
  destination all report one number at render time.
- **Review lane (separate from authoring):** independent adversarial
  code-review pass → APPROVE, 0 critical/high. Its two MEDIUMs were fixed
  before merge: (a) slug-collision dedup in `listCanonicalIndustries` — two
  labels slugifying alike ("AI/ML" vs "AI ML") would otherwise both link to one
  URL and leave the loser a silently-unreachable page; (b) the single-count-
  source consistency fix above. Two LOWs consciously deferred: sub-second
  `CURRENT_DATE` (Postgres) vs `new Date()` (Node) drift at a quarter boundary
  (self-heals on the 6h ISR window; both UTC), and the full momentum table
  fetched per detail render (intentional — the same RPC feeds the index; ISR-
  amortized; scoping it would need a new migration for a ~50-row read).
- **Verified:** web `lint` + 230 unit tests (+11 new: slug helpers, quarter
  windowing, `fundingGrowth`) + webpack `build` + `check:bundle` (no leaks) +
  `test:e2e` (15, +2 smoke: index 200, non-canonical 404). Full
  `statusCheckRollup` green (secrets/pipeline/web/Vercel) before merge.
- **Next in the SEO program:** `/trends` dashboard (reuses the same `0036` RPCs
  + `ThemeFundingChart`), then `/vs/[a]/[b]`, RSS + `/c` timeline, market map.

## PR #166 — `/trends` funding dashboard (merged 2026-07-13)

SEO growth-engine **slice 2**, on the `0036` momentum RPCs.

- **Surface:** `/trends` = catalog-wide funding-by-quarter chart (12-quarter
  macro view, reuses `ThemeFundingChart` via `fundingByQuarter(12)` with no
  industry scope) + **hottest industries** by trailing-2-quarter growth + a
  **biggest-recent-rounds** board. Deliberate split: `/trends` ranks industries
  by *growth* ("what's heating up") while `/industry` ranks by *absolute recent
  funding* (a browse order), so the two SEO surfaces never say the same thing.
- **New code:** `web/lib/queries.ts` `listBiggestRecentRounds(limit, sinceDays)`
  — largest dated+amounted rounds in the last 180d for non-excluded companies,
  **de-duped on `(company, round_type, amount)`** (the per-company key from
  `dedupedRoundsTotal` extended with the slug) so a re-reported round can't fill
  the board with copies of one mega-round; over-fetches `limit*4` then trims.
  Mirrors the `listRecentFundings` join pattern. `web/app/trends/page.tsx` (new).
- **404 guard:** hottest industries are intersected with the canonical bucket
  set (`listCanonicalIndustries`) before linking, so a funded-but-sub-canonical
  industry (which has no `/industry` page) is never linked.
- **Review-cleanup refactor:** the growth-chip tone helper was copy-pasted in
  `/themes`, `/industry`, `/trends`; extracted to `lib/format.ts` as
  `growthToneClass` (unit-tested) and imported from all three.
- **Review lane:** independent adversarial pass → APPROVE, 0 critical/high/
  medium; its two LOWs (the dup helper above; an imprecise dedup-key docstring)
  fixed before merge.
- **CI infra note (not code):** the PR's first `lint`-workflow runs failed at
  GitHub's **"Set up job"** runner-provisioning step — `secrets` in one run,
  `pipeline` in the other, each passing in the sibling run; `web` green in both.
  Pure runner-allocation flake (distinct from the `onnxruntime-node` nuget
  `ETIMEDOUT` install flake seen on #165's docs commit). `gh run rerun --failed`
  on both → full rollup all-green, THEN merged. Verified the complete rollup
  JSON (never grep/tail) — a "Set up job" red is easy to mistake for a real
  failure and equally easy to mistake a masked one for green.
- **Verified:** lint + 233 unit tests (+3 `growthToneClass`) + webpack `build`
  (`/trends` static@6h) + `check:bundle` (no leaks) + `test:e2e` (16, +1 smoke).

## PR #167 — `/vs/[a]/[b]` head-to-head compare pages + shared `CompareTable` (merged 2026-07-13)

SEO growth-engine **slice 3**.

- **Surface:** `/vs/[a]/[b]` renders two listed companies side by side via the
  shared `CompareTable`. **Conservative indexing** (the owner-approved call): a
  page is indexable ONLY when the two are a **resolved competitor edge AND ≥1
  side has real funding**; every other pair renders but `noindex,follow`s — the
  long tail of arbitrary pairs would be thin, near-duplicate doorway pages.
  Pairs are unordered: both URL orderings render identical content and canonical
  to the lexicographically-sorted URL. 404 on self-compare or a non-listed side.
- **`CompareTable`** extracted verbatim from `/compare` (behavior-preserving —
  reviewer diffed it char-identical); `/compare` + `/vs` both render it.
- **New code:** `lib/vs.ts` (pure — `canonicalVsPair`, `vsPath`);
  `lib/queries.ts` `areCompetitorsBySlug` — resolved-edge probe in EITHER
  direction via a PostgREST `or(and(company_id.eq.A,competitor_company_id.eq.B),
  and(company_id.eq.B,competitor_company_id.eq.A))` filter. This is the first
  nested-`and()`-in-`.or()` in the repo — syntax verified against the live
  supabase docs (context7) before shipping, not from memory. UUIDs are
  DB-sourced (no injection surface).
- **Discovery, no pair-sitemap:** internal "Compare X vs Y" links on
  `/alternatives/[slug]` resolved competitors are the crawl path to the
  indexable pairs. A full pair-sitemap would be O(edges) huge and mostly
  noindex; internal links + the noindex gate is the conservative equivalent.
- **`loadVs` wrapped in React `cache()`** so `generateMetadata` + the page share
  one fetch per render (the double-fetch the /industry + /themes detail pages
  still eat — worth retrofitting there later).
- **Review lane:** independent adversarial pass → APPROVE, 0 critical/high; its
  one MEDIUM (the double-fetch) fixed via `cache()` above. Verified the
  excluded-company path is airtight (an excluded side → `<2` listed → 404 before
  the edge signal is ever used).
- **Verified:** lint + 237 unit tests (+4 `vs` helpers) + webpack `build` (`/vs`
  dynamic on-demand, no `generateStaticParams`) + `check:bundle` (no leaks) +
  `test:e2e` (18, +2 smoke: self-404, unknown-404). Full `statusCheckRollup`
  all-green (no infra flake this time) before merge.

## PR #168 — fix: disambiguate the competitors embed (broken /compare, 404 /vs) (merged 2026-07-13)

**A latent prod bug the /vs launch surfaced.** Verifying /vs on prod, every
pair 404'd. Root cause was NOT /vs: `getCompaniesForCompare` embeds
`competitors(competitor_name, rank)` with no FK hint, but the `competitors`
table has TWO FKs to `companies` (`company_id` + `competitor_company_id`,
models.py:470/478). PostgREST can't resolve the ambiguous embed → **400s the
whole query** → the helper's catch returned `[]`. So `/compare` had rendered
"None of those companies are listed" for EVERY pair since the compare feature
shipped (#91), and `/vs` inherited it as a 404 (`loadVs` saw <2 listed).

- **Why it stayed hidden:** the `/compare` e2e smoke only asserts the empty-
  state + unknown-slug paths; the data-backed smoke that would've caught it is
  gated behind `SMOKE_HAS_DATA=1`, which CI doesn't set. A mocked unit test
  can't see a PostgREST query-shape error either. Test-coverage gap noted.
- **Fix (one line):** `competitors!company_id(competitor_name, rank)` — hint the
  owning-company FK, mirroring `getAlternatives`' `companies!competitor_company_id`.
- **Verification (the important part):** local lint/test/build are blind to this
  (mocked), so proof was the **Vercel preview deploy against real Supabase**:
  `/compare?slugs=anthropic,cresta` renders both columns + all rows; `/vs/
  anthropic/cresta` → 200, indexable (a funded competitor edge); `/vs/0x-labs/
  100ms` → 200 but `noindex,follow` (arbitrary non-edge pair) — confirming the
  conservative-indexing gate end-to-end on real data. Then confirmed on prod.
- **Gotcha for next time:** a PostgREST/supabase-js embed of a table with ≥2 FKs
  to the same parent is ambiguous and 400s the ENTIRE request — always hint
  `child!fk_column(...)`. Silent because the helper swallows the error to `[]`.

## PR #169 — `/feed.xml` RSS feed (merged 2026-07-13)

SEO growth-engine **slice 4a** (the RSS half of "RSS + `/c` timeline"; the
timeline half is a separate PR because it destructively redesigns `/c`).

- **Surface:** `/feed.xml` — an RSS 2.0 firehose of the catalog's newest events,
  funding rounds + news articles interleaved newest-first, capped 40. On-site
  only (email deferred this quarter — owner's call).
- **New code:** `lib/rss.ts` (pure, unit-tested — `buildRssFeed`, `xmlEscape`,
  `toRfc822` RFC-822 dates); `lib/queries.ts` `listRecentNews` (non-excluded,
  dated, newest-first) paired with the existing `listRecentFundings`;
  `app/feed.xml/route.ts` (route handler, 6h ISR, `application/rss+xml`,
  empty-but-valid feed without Supabase — never 500s). RSS auto-discovery
  `<link rel="alternate">` in the layout head + a footer link.
- **Proportionate review:** small, additive, well-tested (escaping,
  excluded-company filtering, feed validity all under test) — shipped without a
  subagent review pass, unlike the data-correctness/destructive slices.
- **Verified:** lint + 244 unit tests (+7 rss) + webpack build (`/feed.xml`
  static@6h) + `check:bundle` + e2e (19, +1 smoke: 200 + content-type + valid
  envelope). Full rollup all-green before merge.

## PR #170 — `/c` unified event timeline (merged 2026-07-13)

SEO growth-engine **slice 4b** — the destructive half of the RSS+timeline item.
Replaced the separate Funding History table + News list on `/c/[slug]` with one
"Timeline" (`components/EventTimeline.tsx`): funding rounds + news interleaved,
funding entries keeping full detail (round type, amount, post-money valuation,
lead/other investors, low-confidence pill). Deleted `FundingHistory.tsx` +
`News.tsx` (only `/c` used them) and migrated their tests.

- **Owner deferred the design to me**; I took Option 1 (unified timeline, funding
  stays rich) and — because this redesigns the most-viewed page type — did NOT
  merge on green CI alone. Verified the RENDER on a Vercel preview against a real
  funding-heavy page (Anthropic).
- **Preview-verification earned its keep** — it exposed a UX bug invisible to
  unit tests + green CI: Anthropic's rounds are **undated** (LLM-extracted rounds
  often are), so a naive chronological merge buried the $65B Series H BELOW ~45
  dated news items. Fix: a **tiered sort** — undated funding leads (tier 0),
  dated events run chronologically (tier 1), undated news trails (tier 2). Dated
  funding still interleaves naturally.
- **Adversarial review** (separate lane) → COMMENT, 0 critical/high; two MEDIUMs
  fixed before merge: a bare "Led by —" when a round had only non-lead investors,
  and the post-money valuation losing its money-green color vs the old table.
  Added `aria-label` on the `<ol>` + tests for the tiered order and the investor
  case. LOWs (freshness riders dropped; table→list a11y) judged acceptable.
- **Lesson reinforced:** for a destructive change to a core page, preview-verify
  the actual render on real data before merge — green CI + mocked unit tests said
  nothing about the buried-funding ordering. See [[nous-postgrest-ambiguous-embed]]
  (the last time real-data verification caught what CI couldn't).
- **Verified:** lint + 247 unit tests (+ EventTimeline suite) + webpack build +
  `check:bundle` (no leaks) + e2e (19) + the two-round preview render. Full
  rollup all-green before merge.

## PR #171 — docs: ROADMAP.md living doc + doc-maintenance convention (merged 2026-07-13)

- Product-strategy pass (product owner + CTO-partner brainstorm), not a
  fable5-series code change — logged here to keep the one-entry-per-PR record
  whole.
- **`ROADMAP.md` (new):** strategic layer above `BACKLOG.md`, as Now/Next/Later
  horizons. North star fixed with the owner: **data quality first, then depth.**
  Records the sourcing moat and the **"route around, don't evade"** principle for
  the ~890 husk companies (proxy/Cloudflare evasion rejected on principle: it
  contradicts the sourcing moat, rots on Cloudflare updates, and is unnecessary
  since husks are prominent enough to resolve via news/portfolio outbound links →
  Wikidata → Common Crawl). Deliberately-deferred bets carried forward with
  reasons.
- **`CLAUDE.md`:** new "Keeping the docs current" section — which-doc-when table
  making doc upkeep part of "done" (backlog / roadmap / handoff / worklog).
- **`BACKLOG.md`:** ROADMAP cross-link + dated "Now horizon" section. New work
  (husk re-mining stage, data-quality dashboard, per-company completeness score)
  cross-references existing open entries (formatUsd, hq_state, report-incorrect-
  data, thin-tag hygiene) instead of duplicating them.
- **Verified:** docs-only, no code paths; full CI rollup (pipeline / web /
  secrets / Vercel) green before squash-merge.

## PR #172 — ci: resolve-website-fallback dispatch workflow (merged 2026-07-13)

Enabling infra for #174. `workflow_dispatch` requires a workflow to exist on the
default branch before it can be triggered, so the dispatch lever (dry-run
default + backfill) landed on main first — letting the pre-merge dry run be
triggered against the feature branch's code via `--ref`. Inert until dispatched;
shares the `nous-pipeline-db` concurrency group. Verified: full CI rollup green.

## PR #173 — feat(db): migration 0037 website provenance + fallback rotation stamp (merged 2026-07-13)

Prerequisite schema for #174, landed on main ahead of the stage. **Why split:**
a migration whose file is absent from the branch the 3h cron runs would crash
its `alembic upgrade head` ("can't locate revision 0037"), so in this
Actions-only-prod repo the migration must reach main before prod gets it — and
the pre-merge dry run needs the columns it queries.
- `companies.website_source` + `website_source_url` — per-website provenance
  (mirrors the `status_source_url` / `total_raised_source_url` sibling-column
  convention). NULL for the legacy cohort; the first per-website provenance the
  schema has carried.
- `companies.website_fallback_checked_at` (indexed) — the fallback resolver's
  own rotation/back-off stamp, deliberately separate from `website_resolved_at`
  (resolve-homepages') so the two resolvers rotate independently.
- Also gave the dispatch workflow its own "Apply migrations" step.
- **Verified:** up/down/re-up against a local pgvector container; full CI rollup
  green (DB-gated suite runs against CI's pgvector with 0037 applied).

## PR #174 — feat(pipeline): resolve-website-fallback husk re-mining (merged 2026-07-13)

ROADMAP Now #1. Resolves website-less husks from non-origin sources instead of
fighting Cloudflare. New idempotent `resolve-website-fallback` stage, first
accepted candidate wins:
- **wikidata** — Wikidata "official website" (P856) for a name + org-type +
  country matched entity. Three precision gates so a name collision self-rejects
  (validated live: the "Clay" family-name and "Hebbia" no-website entities
  correctly yield nothing). Robots: the JSON API at `/w/api.php` is treated as
  robots-exempt narrowly (Wikimedia's sanctioned programmatic interface;
  contact-UA + 1 req/sec still honored), mirroring the Google-News-RSS exemption.
- **news_outbound** — the company's own homepage link in the body of an
  already-sourced news article, re-fetching the *article* (not the origin) and
  matching by domain-label or anchor name.
- **Data-reality correction:** `news_articles.raw_content` and `raw_pages.content`
  store extracted **visible text, not HTML** (models.py), so sources (a)/(b) as
  the roadmap first framed them ("read cached outbound links") can't read hrefs
  from stored rows — the stage re-fetches the article live (still non-origin).
  And VC-portfolio pages aren't cached in `raw_pages` (it's company-scoped), and
  the portfolio adapters already capture `entry.website` at discovery time — so
  a portfolio source is largely redundant for portfolio-discovered husks; not
  built. Common Crawl deferred (weak for name→domain).
- **Provenance, no fetch-validation:** the origin is unreachable by design, so a
  candidate is accepted on its source's entity match (not by fetching it) and
  recorded with `website_source` + `website_source_url`. Every resolution is
  auditable + reversible (repair-wrong-websites + `rejected_urls`, respected
  here). On a hit: website + provenance + `website_resolved_at` +
  `website_fallback_checked_at`. On a miss: only the fallback stamp. $0.

**Dry run (30 prominent husks, prod):** 11 resolved (37%), 0 conflicts, 0
errors, $0. wikidata 9/30, news_outbound 2/30 (disjoint, net +2). 10/11
unambiguously correct (incl. Taxfix→.de, Proxima Fusion→.com — correct foreign
matches; Kraken Technology Group, NOT kraken.com the exchange). One collision:
US-focused "Apex Technologies" matched Wikidata "APEX Technologies (France)".
**Threshold fix from the dry run:** conservative **country cross-check** — reject
a Wikidata candidate only when the company's known `hq_country` and the entity's
mapped P17 country both exist and conflict. Never drops a NULL-country husk (so
Taxfix/Proxima survive); a US company with a known country won't take a
confirmed-foreign site. The Apex husk has NULL country → documented residual
(sourced + reversible), not fixed by dropping correct foreign matches.

- **Wiring:** bounded id'd `pipeline.yml` step before resolve-homepages
  (`--limit 25 --max-runtime-minutes 3`, no new dispatch input — 25-cap full);
  prod drains ~25/run via the cron (gradual = safe first application), the
  dispatch is the faster-backfill lever.
- **Verified:** ruff + mypy clean; full suite 1520 passed (pure selection cores
  + DB-gated stage); two green prod dry runs; full CI rollup green before merge.
- **Lesson:** the GitHub `workflow_dispatch`-must-be-on-default-branch rule plus
  the Actions-only-prod migration-ordering constraint forced a 3-PR split
  (dispatch #172 → schema #173 → stage #174) to run a real pre-merge prod dry
  run. Worth it: the dry run caught the Apex-France collision that unit tests +
  local pgvector couldn't (real-data verification again — cf. [[nous-postgrest-ambiguous-embed]]).

## PR #175 — feat(pipeline): data-quality completeness report (dashboard) (merged 2026-07-13)

ROADMAP Now #2 (+ #5's internal primitive). New read-only, idempotent
`data-quality` stage — the completeness sibling of db-stats (size) and
pipeline-health (freshness) — emits a step-summary report over the shown cohort
(`exclusion_reason IS NULL`):
- **Field completeness %s** — website / description / funding / logo / people /
  location / industry / tags / employees (one per-company Core-select round-trip
  + a `SELECT DISTINCT people.company_id` set; computed in Python).
- **Website provenance** — counts by `website_source` (wikidata / news_outbound /
  legacy-`unattributed`), so #174's re-mining contribution and the wrong-site
  proxy are visible in every run.
- **Completeness score** — new pure `util.completeness` (weighted 0..1,
  `FIELD_WEIGHTS` sum to 1.0, husk-defining fields dominate) aggregated into
  mean + a 4-bucket histogram + husk (<0.25) / fully-complete (==1.0) counts.
  Delivers Now #5 "internal first"; wiring into husk-enrichment ordering + a
  public badge is a follow-up (no behavior change to other stages here).
- **Duplicate rate** (shared `normalized_name`) + **enrichment staleness** buckets.
- **Wiring:** id-free read-only cron step next to db-stats/pipeline-health (can't
  trigger the Vercel deploy). No migration, no writes → clean single PR, no
  schema-ordering ceremony; the report shows in the next cron's step summary.
- **Verified:** ruff + mypy clean; full suite 1526 passed (pure score + DB-gated
  stage: field %s, husk/complete counts, provenance, dupes, staleness); full CI
  rollup green before merge.

## PR #176 — feat(pipeline): normalize hq_state to USPS code (merged 2026-07-13)

BACKLOG "hq_state unnormalized (CA vs California)". `companies.hq_state` was
stored ragged ("California" / "CA" / "ca"); location pages rendered the stored
casing and full-name `/location/California` links 404'd.
- **Canonical form = 2-letter UPPERCASE USPS code.** Chosen because the web
  location route (`web/app/location/[state]/page.tsx`) resolves a segment by
  `decodeURIComponent(seg).toUpperCase()` and `queries.ts` does
  `q.eq("hq_state", state)` — the code is the ONLY form that resolves. Full
  names were unreachable (route uppercases to "CALIFORNIA", nothing stored that
  way). So normalizing full names → "CA" is strictly routing-safe: every
  `/location/CA` that resolves today keeps resolving, and broken full-name links
  start pointing at the working code URL. No web change needed (the `stateAbbrev`
  display helper in `web/lib/format.ts` already collapsed both forms for render).
- **New pure map** `util/us_state.py` — `canonical_us_state(value) -> str | None`
  over the 50 states + DC; case/whitespace tolerant; returns None for foreign /
  territory / garbage so callers never clobber a non-US value. Unit-tested
  (`test_us_state.py`).
- **Write-site** enrich-companies now stores `canonical_us_state(hq_state) or
  raw`, so NEW US data is normalized while non-US strings pass through. Downstream
  of the LLM call → no `PROMPT_VERSION` bump, golden set untouched (the eval
  already `.upper()`s both sides).
- **Backfill** `normalize-hq-state` stage + CLI (`--limit`, `--dry-run`). SELECT
  filters entirely in SQL to rows whose `hq_state` is a US-state spelling ≠ its
  canonical code (self-bounding so `--limit` bounds real work; non-US never
  matches), per-row commit with `StaleDataError` skip (mirrors embed-companies),
  idempotent. Records no new source (pure format op). DB-gated test
  (`test_normalize_hq_state_db.py`).
- **No migration** (content-only; `hq_state` column + index already exist,
  head stays 0037). Not yet wired into cron — backfill is a one-shot lever;
  wiring a bounded step is a trivial follow-up.
- **Verified:** ruff + mypy clean; full suite 1560 passed (34 new: pure
  `canonical_us_state` + DB-gated backfill).

## PR #177 — feat(web): report-data link, exact-$ tooltips, thin-tag noindex (merged 2026-07-13)

ROADMAP Now #3 web polish (three small data-quality items), built by a parallel
agent in the main tree (npm-verified) and adversarially reviewed (APPROVE, 0
blocking) alongside #176.
- **Report incorrect data (per-company):** a quiet muted link below the Sources
  block on `/c/[slug]` via the existing `repoIssueUrl()` helper (repo is public
  now, so the prefilled issue link resolves). Prefills `Data correction: <name>
  (<slug>)` + a body with the page URL and a what's-wrong/correct-value/source
  skeleton. Additive alongside the site-wide footer link (no removed block to
  restore — `repoIssueUrl` had been added but left unused until public).
- **formatUsd exact-dollars tooltip:** `title={formatUsdExact(amount)}` on every
  individual company funding figure (spotlight, `/new`, `/trends`, investor
  table, CompareTable). `formatUsd` output untouched, all null-safe. Aggregate
  cross-company momentum sums skipped (false-precision).
- **Thin single-company tag pages:** `/tag/[tag]` `generateMetadata` sets
  `robots: { index:false, follow:true }` when the tag backs <3 companies —
  `MIN_TAG_COMPANY_COUNT` kept in lockstep with `sitemap.ts`'s existing ≥3 filter
  (the repo had already de-thinned the sitemap, so the threshold matched that
  rather than the looser brief).
- **Verified:** npm lint + 247 tests + build green; full CI rollup green.
- **Orchestration note:** #176 + #177 were implemented in PARALLEL by two agents
  (pipeline in an isolated worktree with uv; web in the main tree with npm —
  disjoint dirs, so no conflict, and no parallel node_modules to blow the
  near-full disk), each flowing straight into an adversarial code-reviewer agent
  before merge. Merged sequentially (pipeline first) with docs consolidated to
  main after, to avoid BACKLOG hunk collisions.

## PR #179 — feat(pipeline): market-map coords stage (compute-map-positions) (merged 2026-07-13)

Pipeline side of the ROADMAP "Market map — `/map/[industry]`" bet: precompute
the per-industry 2D scatter positions so the web reads flat columns and never
runs ML in the Vercel function (the #157 250MB lesson). Web renderer is a
separate follow-up.
- **Migration 0038** (hand-written, chains off 0037): three nullable columns on
  `companies` — `map_x`/`map_y` (double precision, normalized `[0,1]` cohort
  coords; NULL = not positioned) + `map_computed_at` (timestamptz freshness
  stamp). NO index (never a selective WHERE key; the read filters the already-
  indexed `industry_group` and treats `map_x IS NOT NULL` as a within-partition
  predicate — same call as `embedding_text_hash` in 0033). Columns over a side
  table: coords are strictly 1:1 with a company, so a side table would add a
  join+FK for zero cardinality benefit (mirrors the 0028 `latest_round_*`
  denormalization rationale).
- **`compute-map-positions` stage** (`pipeline/compute_map_positions.py`):
  per-`industry_group` with ≥ `MIN_MAP_COMPANIES` (5) shown+embedded companies,
  fits scikit-learn `PCA(n_components=2, svd_solver="full")` over the
  unit-normalized description embeddings (E-1), projects to 2D, pins a
  deterministic sign convention, and per-axis min-max normalizes to `[0,1]²`,
  then writes coords + stamp. Mirrors compute-themes: a `Projector` Protocol
  seam with a real `PCAProjector` (eager `import sklearn.decomposition` in
  `__init__` to fail loud, lazy `PCA` in `project()`) so tests inject a
  deterministic fake and scikit-learn is never needed to run them; id-ordered
  fetch; per-industry incremental commit; Pydantic summary.
- **Determinism (the idempotence contract):** `ORDER BY id` +
  `svd_solver="full"` (exact SVD, deterministic at any cohort size — unlike
  `"auto"`, which switches to the randomized solver above 500 samples) + a
  pinned sign convention (each axis's largest-|score| sample, ties→lowest index,
  forced positive) + deterministic min-max. The sign pin is load-bearing
  *because* min-max encodes sign — negate then min-max yields the mirror
  `1 − x`, so without it two runs could emit mirror-image maps. Degenerate
  (constant) axis → `0.5`.
- **Thresholds/cadence:** `MIN_MAP_COMPANIES = 5` (below themes' 8 on purpose —
  a map has no LLM naming, hence no coherence floor). Per-industry TTL gate on
  `MAX(map_computed_at)` (default 25 days), so the weekly `discovery.yml` step
  (after Compute themes) runs at an effective monthly cadence. `--force` bypasses
  the gate. `$0` — local CPU PCA, no LLM, no network, no new dependency
  (`sklearn.decomposition.PCA` ships in the `scikit-learn` already in the
  `embeddings` group for KMeans).
- **Tests:** `test_compute_map_positions.py` (pure — sign-pin invariance under
  global + per-axis mirror, `[0,1]` endpoints, degenerate-axis→0.5,
  determinism, `unit_normalize`, CLI registration, `FakeProjector`) +
  `test_compute_map_positions_db.py` (DB-gated — migration round-trip,
  full flow, below-threshold NULL, excluded/un-embedded skipped, per-industry
  TTL + `--force`, idempotence/determinism).
- **Web hand-off:** read is `SELECT slug, name, map_x, map_y,
  latest_round_amount, funding_round_count FROM companies WHERE industry_group =
  $1 AND map_x IS NOT NULL`. One visual call flagged for the renderer: per-axis
  `[0,1]` min-max fills the box but exaggerates the lower-variance PC2; switch to
  a single shared scale factor to preserve the true PC1:PC2 variance ratio.

## PR #180 — feat(web): market map at /map/[industry] (merged 2026-07-13)

Web side of the market map (ROADMAP Next #1), paired with #179. Built by a
parallel agent (main tree, npm-verified) and adversarially reviewed (APPROVE, 0
blocking) alongside #179. Shipped as a **static server-rendered SVG** — no client
component, and critically no ML in the Vercel function (the #157 lesson).
- **`web/lib/map-layout.ts`** — pure, deterministic geometry: coord→viewBox
  scaling (degenerate-axis safe), sqrt-scaled funding radius, greedy
  non-overlapping labels. Unit-tested in isolation.
- **`web/components/IndustryMap.tsx`** — server SVG. Each node an SVG `<a
  href="/c/{slug}">` with a `<title>` (name + exact funding), theme-safe CSS-var
  colors, an `sr-only` fallback link list, accessible naming via
  `aria-labelledby` (deliberately NO `role="img"`, which would hide the links).
- **`/map/[industry]`** — on-demand ISR (`revalidate=21600`, no
  `generateStaticParams`), hard-gated to canonical `industry_group` buckets
  (`notFound()` on miss), empty maps `noindex`'d. **`/map`** hub + coords-gated
  sitemap entries.
- **`listIndustryMapNodes` / `listIndustriesWithMapCoords`** — explicit
  `map_x`/`map_y` select, so a pre-migration prod 400s → the existing error path
  → `[]` → the empty-state. **Migration-ordering-for-free**: no feature flag, and
  the web PR was independent of #179 (mergeable in any order); maps enter the
  hub/sitemap only once coords exist.
- **#157 ML-safety proven:** build traces show 0 onnxruntime refs in the `/map`
  routes (vs 1 in the `/companies` control); `EMBEDDER_ROUTES` untouched.
- **Verified:** npm lint + 275 tests + build green; full CI rollup green.
- **Note:** coords populate on the next `discovery.yml` run (TTL-gated,
  effective monthly) once migration 0038 reaches prod (next pipeline cron). Until
  then every map is the empty-state by design. Force earlier by dispatching
  `discovery.yml` after 0038 lands.
- **Orchestration:** #179 (pipeline, isolated worktree, uv) + #180 (web, main
  tree, npm) were scouted, implemented, and reviewed by 6 agents across two
  workflows (2 scout → 2 implement → 2 review), merged sequentially.

## PR #181 — feat(pipeline): momentum ("heating up") score (compute-momentum) (merged 2026-07-13)

Pipeline side of the "Momentum signals" bet (ROADMAP Next #2): score every shown
company's weekly "heating up" momentum so the web can rank a `/trending`
leaderboard and light a "heating up" badge off flat columns (no compute in the
Vercel function). Web side (`/trending` + badge) shipped in parallel as #182.
- **Migration 0039** (hand-written, chains off 0038): three columns on
  `companies` — `momentum_score` (double precision, `[0,1]`; 0.5=flat,
  higher=accelerating, NULL=insufficient data), `momentum_computed_at`
  (timestamptz freshness stamp), `momentum_why` (text[] default `'{}'`,
  pre-worded chips the web joins with " · " and never re-computes). PARTIAL DESC
  index `ix_companies_momentum_score` (`WHERE momentum_score IS NOT NULL`) —
  unlike map_x/map_y (0038) this IS the leaderboard's WHERE + ORDER BY key, and
  the partial keeps it to the scored minority. up+down round-tripped.
- **`compute_momentum.py`** — pure, unit-tested component helpers each →
  `[0,1]` (0.5=flat) or ABSENT: news acceleration (`company_snapshots.news_count_30d`
  recent 2wk mean vs weeks-3–9 baseline, `(recent+3)/(baseline+3)` clipped to
  `[¼,4]`, log-mapped), funding recency (`latest_round_date` exp-decay τ=180d),
  headcount growth (snapshot midpoints, 56d gap). Combined as a
  **weight-renormalized mean over the PRESENT components** (news 0.50 / funding
  0.35 / headcount 0.15) so a missing signal drops out rather than drags;
  all-absent → NULL (never fabricated). Anchored to `as_of_week` for
  determinism/idempotence; every shown company is (re)written each run (value OR
  NULL) so a faded signal clears its stale score. Batched `begin_nested` commits.
- **`compute-momentum` CLI** (`--as-of-week` for backfill/determinism) — records
  a `pipeline_runs` row (`flag_empty` off: a young catalog legitimately scores
  few). Wired into `discovery.yml` AFTER "Snapshot companies" (weekly, NOT
  TTL-gated, plain `uv sync` — $0, no LLM/network/scikit-learn); given `id:
  momentum` so a fresh score triggers the Vercel redeploy via the deploy gate,
  like the `map` step.
- **Web hand-off (shared contract):** read is `SELECT slug, name, momentum_score,
  momentum_why, ... FROM companies WHERE momentum_score IS NOT NULL ORDER BY
  momentum_score DESC`. `momentum_why` is display-only (join with " · "); the
  badge lights at a conservative `momentum_score` threshold (~0.65, calibratable
  constant on the web side). NULL rows are hidden from `/trending`.
- **Verified:** `ruff` + `mypy src` clean; `alembic upgrade→downgrade→upgrade`
  round-trips 0039; full `pytest -q` = **1625 passed** (44 new momentum tests, DB
  container on `:55432`).
- **Launch reality (flag for the reviewer):** the `company_snapshots` table is
  new — until ~6 weekly rows accrue per company the news component is ABSENT for
  most, so early scores are funding-recency-dominated and self-enrich as history
  builds (no code change needed). Populates on the next `discovery.yml` run once
  0039 reaches prod.

## PR #182 — feat(web): /trending "Heating up" momentum surface (merged 2026-07-13)

Web side of momentum (ROADMAP Next #2), paired with #181; built by a parallel
agent (main tree, npm-verified) and adversarially reviewed (APPROVE, 0 blocking).
The web only READS the pipeline-computed `momentum_score` — no computation.
- **`/trending`** ("Heating up") — a ranked `CompanyCard` grid ordered by
  `momentum_score` desc, each with a pipeline-worded "why" line, "Momentum as of"
  rider, on-demand ISR, empty-state.
- **`🔥 Heating up` badge** (`MomentumBadge`, `MOMENTUM_BADGE_THRESHOLD=0.65` on
  the `[0,1]` score) on `CompanyCard` + the company detail header. Badge threshold
  and query floor are separate documented calibratable constants.
- **`listHeatingUpCompanies`** — shown-cohort + scored-only, migration-order-free
  (explicit `momentum_score` select → pre-migration 400 → `[]` → empty-state), so
  #182 was independent of #181. Momentum-specific naming throughout (NOT the
  existing spotlight `TrendingCompany`/`getTrendingCompanies`). Optional
  `CompanyCard` props → zero regression on the 8 other card call sites (tested).
- Nav + footer + sitemap links.
- **Verified:** npm lint + 292 tests + build green; full CI rollup green.
- **Note:** `/trending` + badges light up automatically on the next ISR
  revalidate once migration 0039 reaches prod and the first weekly
  `compute-momentum` run scores companies.
- **Git incident:** the web branch (main-tree agent) got reset to main on origin
  mid-run; the work commit `a05fbca` survived locally and was restored by
  fast-forward push before the PR — a reminder to re-verify branch tips after a
  parallel main-tree agent finishes. The 1wk-grammar review nit on #181 was fixed
  in a follow-up commit before merge.

## PR #183 — feat(web): per-entity RSS feeds (company / industry / investor) (merged 2026-07-13)

ROADMAP Next #3. Fanned out the global `/feed.xml` firehose to three per-entity
scopes — "watch this" without accounts, $0, works immediately against existing
data (no migration/cadence dependency). Built by a web agent (main tree,
npm-verified) + adversarial review (APPROVE, 0 blocking).
- **`/c/[slug]/feed.xml`** (one company's funding+news, reuses the `/c` timeline
  query), **`/industry/[group]/feed.xml`** (canonical-gated via
  `resolveIndustrySlug`), **`/investor/[slug]/feed.xml`** (the investor's
  portfolio companies). All route handlers, `revalidate=21600`, correct
  `application/rss+xml` + CDN cache headers, newest-first, stable guids matching
  the global scheme.
- **Shared `lib/rss-items.ts`** (row→`RssItem` mappers + `mergeFeedItems` +
  `rssResponse`) — the global `/feed.xml` refactored onto it (byte-identical),
  so the 4 feeds can't drift. New scoped queries mirror the global
  `listRecentFundings/News` + one `industry_group`/`slug IN` filter, shown-cohort
  only (no excluded-company leakage, reviewer-verified).
- **Discovery:** each entity page adds `<link rel="alternate">` (via
  `generateMetadata`) + a visible "Follow via RSS" link (`RssLink`).
- Each feed empty-but-valid on missing Supabase / absent entity (never 500);
  404 on a configured-but-unknown/non-canonical entity.
- **Verified:** npm lint + 318 tests + build; XML well-formedness asserted via
  DOMParser in route tests; full CI rollup green.
- Follow-ups (deferred, BACKLOG): investor-feed portfolio over-fetch, a feed hub
  / subscribe hint.

## PR #184 — feat(pipeline): career-history-probe ($0 talent-flow feasibility gate) (merged 2026-07-13)

The evidence gate for ROADMAP Next #4 (talent-flow), before any LLM spend. A $0,
read-only regex diagnostic (third read-only DB instrument alongside
db-stats/data-quality) measuring whether scraped bios carry **named** prior
employers. Built + reviewed by 2 agents; dispatched once against prod.
- **Prod result (denominator = 2,210 shown companies with pages):** bio section
  69.5%; any career signal 24.6%; **named prior-employer 17.7% (SQL upper bound)
  / 22.3% marquee-sample**, but example captures were ~40% noise (titles like
  "VP"/"CTO", sentence-starters) → true rate ~13–15%. Real orgs present (Intel,
  IBM, NVIDIA, Cisco, FireEye, SambaNova, Softbank, OakNorth) but sparse and
  mostly non-catalog non-startups.
- **Verdict:** the rich talent-flow *graph* is NOT well-supported by current
  scrape data (thin + prior orgs mostly non-catalog). A per-company "founder
  background" rider on the ~1-in-6 pages that name a pedigree is feasible via a
  bounded LLM extraction (~$0.05 dry run, ~$6.50 one-time backfill) — a
  value-vs-cost call, deferred to the owner. See ROADMAP Next #4.
- **Tooling shipped:** `career-history-probe` stage + CLI + `career-history-probe.yml`
  dispatch (read-only, no DEEPSEEK key). Reusable to re-measure as scrape
  coverage grows. Verified: ruff+mypy, 1665 passed (40 new); full CI green.
- **Process note:** this is the husk-style "evidence before plumbing" gate — $0
  measurement first, LLM spend only if the number clears the bar. It didn't
  clearly clear (~15% vs the ~30% green-light), so the LLM extraction was NOT
  built pending an owner value/cost call.

## PR #185 — feat(pipeline): extract-career-history dry-run (talent-flow founder-background gate) (merged 2026-07-14)

Owner approved building the talent-flow **"founder background" rider** (ROADMAP
Next #4-lite) despite the thin #184 signal, accepting the new DeepSeek line. This
is the husk-style evidence gate before the full pipeline: a bounded DeepSeek
extraction that, per shown company with a leadership roster + scraped pages,
pulls each founder/exec's PRIOR employers — empty-not-fabricate for the ~85%.
- New `career_history` prompt (PROMPT_VERSION, PriorRole/PersonCareer/
  CareerHistoryExtraction, hardened roster-attributed template, validators that
  drop pedigree-less/off-roster noise; a null prior company drops only that role,
  never the whole company's extraction).
- `extract-career-history --limit --dry-run` stage + `extract-career-history.yml`
  dispatch (DEEPSEEK_API_KEY, dry_run default true, limit default 20). Dry-run
  roster-matches, renders a yield table (off-roster + self-reference fabrication
  proxies, example moves) + the $ via emit_run_telemetry, writes nothing.
- Adversarially reviewed (4 lenses → verify): 3 confirmed yield-fidelity defects
  fixed (self-reference inflation, duplicate-people double-count, null-company
  fatal parse). Verified: ruff+mypy, 1138 passed; full CI green.
- **Prod dry run (20 top-funded):** 50% with ≥1 named prior, 34 people, 69 edges,
  **0 off-roster, 0 self-ref, 0 errors, $0.0253** — clean named employers
  (Rodrigo Liang → Sun/Oracle/HP, Tom Mueller → SpaceX, Drew Durbin → Sendwave).
  Cleared the gate → full build greenlit.

## PR #186 — feat(db): migration 0040 career_moves + CareerMove model (merged 2026-07-14)

Schema-only PR landing the `career_moves` table ahead of the writer (the 3-PR
husk split: dispatch workflow #185 → this migration → apply stage), so a
migration absent from main can't crash the cron's `alembic upgrade head`.
- `career_moves`: company_id FK CASCADE (indexed), person_name +
  person_normalized_name (indexed; NO FK to people.id — people is wiped every
  enrich run, so keying to company_id + normalized name decouples from that
  churn), prior_company_name (verbatim), prior_company_id FK companies ON DELETE
  SET NULL (the in-catalog graph edge; SET NULL so deleting the prior company
  drops only the link, never the fact), prior_role, start/end_year (SmallInt),
  source_url, extraction_prompt_version. UNIQUE (company_id,
  person_normalized_name, prior_company_name) — the replace-style idempotency key.
- Verified on a pgvector:pg15 container: upgrade/downgrade round-trip, `\d`
  confirms columns/indexes/FKs, DB suite 1691 passed. New DB-gated tests cover
  the unique key + CASCADE-vs-SET-NULL delete semantics.

## PR #187 — feat(pipeline): extract-career-history apply path + golden set (merged 2026-07-14)

Turns the #185 dry-run stage into the persisting pipeline, gated by a new golden
set.
- **Apply path:** version-gated selection (career_extracted_prompt_version IS
  NULL OR < PROMPT_VERSION) + --limit; replace-style per company (DELETE
  career_moves → INSERT edges → stamp → commit). The per-company stamp
  (**migration 0041**, indexed) makes the ~85% empty-bio companies idempotent —
  career_moves rows alone can't tell "never extracted" from "extracted, empty",
  so without it every run would re-bill DeepSeek for the empties. prior_company_id
  resolved by EXACT unique normalized-name match (high precision). Edges collapsed
  to one per (person, prior company). record_pipeline_run (flag_empty=False).
- **Prompt** 2026-07-13.2: named-company-or-null (drops the #185 descriptive-phrase
  tail) + a schema length-cap.
- **Golden set:** career_history PromptSpec + scorer gating empty_accuracy (the
  empty-not-fabricate dial), people/moves P/R, and a DEDICATED per-token grounder
  (the shared grounding_fraction skips each fragment's first word — blind to
  short/leading names). 16 hard-case fixtures; CaseSpec gains a roster.
- Adversarially reviewed (4 lenses → verify): 2 confirmed defects fixed — a
  post-rollback MissingGreenlet crash (rollback expires the identity map → the
  loop now drives off ids + re-get; regression-tested) and the vacuous grounding.
  Verified on pgvector:pg15: migration round-trip, DB suite 1697 passed.
- **Live golden re-record (#189, eval-record.yml):** replaced the simulated
  recordings with real deepseek-chat — `parse_rate 1.0, empty_accuracy 0.937,
  people_precision 0.928/recall 1.0, moves_precision 0.894/recall 1.0,
  grounding 1.0` (zero fabrication live). Baseline strengthened to reality.

## PR #188 — feat(web): founder-background rider on /c/[slug] (merged 2026-07-14)

The web surface: a "Founder background" section listing where each founder
worked BEFORE this company, grouped by person, linking to /c/[prior_slug] when
the prior employer resolves to a shown catalog company.
- `getCareerMoves` reads career_moves with the REQUIRED FK-hint embed
  `companies!prior_company_id(...)` (two FKs to companies → un-hinted 400s
  "ambiguous"). Migration-order-free: error → [] → hidden until the table lands.
  Excluded prior companies keep their name as text, drop the link.
- `FounderBackground` server component (omit-when-empty; ~1-in-6 pages render
  anything, by design). Tenure renders honestly — a prior employer with an
  unknown end year shows "from 2005", never "2005–present" (that would fabricate
  an unsourced current-employment claim).
- Adversarially reviewed (2 lenses → verify): 1 confirmed defect fixed (the
  "present"/"?" tenure fabrication), regression-tested. Verified: npm lint +
  test (328 passed) + build.

## PR #190 — feat(web): portfolio-momentum lens on /investor/[slug] (investor depth) (merged 2026-07-14)

ROADMAP Next **#5 (investor depth)** — turn the investor directory from a list
into a lens. The co-investment lens ("frequently co-invests with") already
shipped (`getCoInvestors`, read-time); this adds the **portfolio-momentum** lens:
how many of an investor's portfolio companies are heating up right now, and the
hottest few. $0, read-time — reuses the pipeline `momentum_score` (#181), no new
data/LLM.
- `getInvestorPortfolioMomentum(slug)`: aggregates `momentum_score` over the
  investor's DISTINCT shown portfolio companies, unioned across BOTH link paths
  (`company_investors` + `funding_round_investors`→`funding_rounds`→`companies`)
  and deduped by slug. Returns scoredCount / heatingUpCount (≥ the shared
  `MOMENTUM_BADGE_THRESHOLD` 0.65) / meanMomentum / topHeatingUp. Both embeds are
  unambiguous (one FK each) so no FK hint; fetch capped 2000/path (mega-fund).
  Migration-order-free; a single-path failure still yields a partial aggregate.
- `PortfolioMomentum` server component — omit-when-cold, links the hot companies
  with their momentum "why" chips (reuses /trending wording).
- Adversarially reviewed (2 lenses → verify): 1 confirmed grammar defect fixed
  (noun agreed with the numerator not the denominator), regression-tested.
  Verified: npm lint + test (336 passed) + build.
- **Follow-ups (BACKLOG):** "who's leading rounds in industry X right now" (a
  separate industry-page surface); a global co-investment meta-graph.

## PR #191 — feat(pipeline): stored completeness score for the web provenance badge (merged 2026-07-14)

ROADMAP **Later #1 (Provenance UI)**, PR **1 of 3** — the $0 pipeline half that
lets the web render a completeness badge without re-implementing scoring in TS.
Makes the "every fact is sourced" moat a visible feature (see
`docs/superpowers/specs/2026-07-14-provenance-ui-design.md`).
- **Migration 0042** (hand-written, off head 0041): `companies.completeness_score`
  (Float 0..1) + `completeness_computed_at`. No index — read per-company for a
  page badge, never a WHERE/ORDER BY key (unlike `momentum_score`'s leaderboard).
  Up→down→up round-trip container-verified on `pgvector/pgvector:pg15`.
- **`compute-completeness` stage** — writes the score for every *shown* company
  (same cohort as `compute-momentum`) via `util.completeness` (THE scorer the
  data-quality report already aggregates — no second implementation). $0,
  deterministic, idempotent, batched `begin_nested` commits mirroring
  `compute-momentum`. Wired into `discovery.yml` after momentum with an `id` so a
  fresh score triggers the Vercel deploy gate (rendered surface).
- **Single source of truth** — extracted `completeness_fields()` (pure,
  primitives only) as the one raw→flags mapping; refactored `data_quality.py`
  onto it so the stored column and the internal report can't drift (its DB test
  stayed green — behavior-equivalent).
- **Trust-safety (from adversarial review):** a company that EXITS the shown
  cohort (loses both description and funding, or becomes excluded) has its score
  cleared to NULL, so a stale "richly documented" badge can never render — a
  deliberate divergence from `compute-momentum` (a stale momentum chip is benign;
  a stale provenance claim is a false trust claim). The clear-stale UPDATE's WHERE
  is the exact negation of the scoring SELECT's shown predicate (factored into
  `_shown_predicate()` so they can't drift).
- Adversarially reviewed (4 dimensions → per-finding verify): 3 confirmed nits,
  all addressed (the exit-cohort clearing above + two doc-accuracy fixes).
  Verified: ruff + mypy + pytest (**1714 passed**, full suite with DB attached);
  migration round-trip + real-CLI smoke (full→1.0, thin→0.20, mean 0.6).
- **Next (PRs 2 & 3, independent web PRs):** the "Data & provenance" panel on
  `/c/[slug]` (positive-only badge, "last verified", sourcing line); granular
  per-fact source superscripts + source-type labels + confidence tooltips.
- **Shared debt noted:** `momentum_score` has the identical exit-cohort staleness
  (its badge renders for husks that lost their signal) — accepted for momentum,
  but worth revisiting if it ever reads as a trust claim.

## PR #192 — feat(web): Data & provenance panel on /c/[slug] (merged 2026-07-14)

ROADMAP **Later #1 (Provenance UI)**, PR **2 of 3** — the web half's first
surface. A "Data & provenance" panel on `/c/[slug]` that makes the moat visible,
reading PR 1's stored `completeness_score` (#191) — the web never re-derives the
scorer.
- New `ProvenancePanel` server component (rendered just before `<Sources>`):
  **positive-only completeness badge** (`≥0.75` "Richly documented", `0.5–0.75`
  "Well documented", `<0.5`/null → no badge — never a negative gap badge);
  **"Last verified N days ago"** = read-time MAX over the present freshness stamps
  (`last_enriched_at` + the `*_checked_at`/`_resolved_at` columns), `title` = exact
  date, omitted when none present; a **sourcing line** anchor-linking to
  `#sources`. Omit-when-empty; muted `MomentumBadge`/`StatusBadge` vocabulary,
  light+dark tokens. Shared exported thresholds (`COMPLETENESS_WELL/RICH_THRESHOLD`
  + `completenessLabel()`), mirroring `MOMENTUM_BADGE_THRESHOLD`.
- **Migration-order-free:** `getCompanyBySlug` unchanged (`.select("*")` picks up
  the new columns post-migration); `CompanyRow` gains 7 optional+nullable fields;
  absent columns → panel hides.
- **Trust-safety fix (adversarial review, 3 dims → verify):** `hasSources` was
  gated on raw `citations.length > 0`, which diverged from what `<Sources>`
  renders — it drops citations whose URL fails `new URL()`, and the pipeline
  stores scheme-less bare domains (`company.website` = `acme.com`, the
  total_raised / leadership source fallback). A company whose only citations were
  unparseable showed "every figure links to a recorded source" pointing at a
  `#sources` anchor that didn't exist (dead link + false claim). Fixed by
  exporting `hasRenderableCitations()` — the SAME `hostname()`-survival predicate
  `<Sources>` filters on — and gating on it.
  Verified: npm lint + test (**352 passed**) + build.
- **Next (PR 3/3):** granular per-fact source superscripts next to each sourced
  figure, source-type labels in `Sources`, and `extraction_confidence` tooltips
  on funding rounds (visible pill only for `low`).

## PR #193 — feat(web): granular per-fact sourcing on /c/[slug] (merged 2026-07-14)

ROADMAP **Later #1 (Provenance UI)**, PR **3 of 3** — completes the owner-approved
3-PR MVP. The finishing layer of per-fact provenance.
- **Inline source superscripts** (`SourceLink`): a subtle `↗` next to total
  raised, status, website, and each funding row → that fact's recorded source.
  Self-omitting when the URL is absent/unparseable (the pipeline stores
  scheme-less bare domains) — a "source" affordance never goes nowhere.
- **Source-type labels** in `Sources`: a muted `· News/Website/Wikidata/VC
  portfolio` tag per citation, inferred from the host with the `website_source`
  enum as DB ground truth. Unknown host → NO label (never a guessed attribution).
- **Confidence transparency** in `EventTimeline`: a `title` tooltip on ALL
  funding rounds; the visible pill stays low-only. `CompanyRow` gains
  `website_source?` / `website_source_url?`.
- Also fixed a **pre-existing NUL byte** in `Sources.tsx` (from #192) that made
  git treat the file binary — behavior-identical de-dupe, now clean UTF-8.
- **Adversarial review (3 dims → verify): 4 confirmed, all fixed.** [medium/a11y]
  the `↗` glyph was `text-ink-faint` (~1.42:1, below WCAG's 3:1 for an interactive
  control — invisible to low-vision/touch) → `text-ink-muted`+`hover:text-ink`;
  [nit] `align-super` is inert on flex children → raise via `relative`
  positioning (consistent across all four placements); [nit/moat]
  `citationSourceType` now rejects non-http(s) (an `ftp://` URL no longer gets a
  "News" tag); [nit] the Website/Wikidata/VC-portfolio labels were unreachable
  (the page never cited `website_source_url`) → now cited like total-raised/status,
  proven by a page-render test. Verified: npm lint + test (**375 passed**) + build.
- **GOTCHA (design-system a11y debt, logged not fixed):** `text-ink-faint`
  (~1.42:1 on light) is used pervasively (~30 sites, e.g. `app/page.tsx:253` "+N
  more") for de-emphasized supplementary text — below WCAG AA. Fixed the two
  trust-critical provenance instances here; a system-wide token pass is a
  separate follow-up.

## Provenance UI MVP COMPLETE (2026-07-14, PRs #191–#193)

The owner-approved 3-PR MVP (spec `docs/superpowers/specs/2026-07-14-provenance-ui-design.md`)
is shipped: the moat ("every fact is sourced / we don't hallucinate") is now a
VISIBLE, positive, trust-building feature on `/c/[slug]` — a threshold-gated
completeness badge, a read-time "last verified", per-fact source superscripts,
typed citations, and confidence transparency, all $0 (surfaces existing data; no
LLM). **Remaining (optional, separate bet): the DeepSeek source-verification pass
("✓ Verified against source") — husk-style, dry-run-gated, flagged to the owner
before building (material DeepSeek volume).**

## PR #194 — feat(web): group funding coverage under its round in the timeline (merged 2026-07-14)

Owner-flagged UX debt: the `/c/[slug]` Timeline was cluttered — because
`ingest-news` only ingests funding announcements, the "news" IS the funding
coverage, so one well-covered round rendered as the round PLUS its primary
article again PLUS every outlet's near-duplicate article, each a separate entry
(uncapped, no news↔round link, no clustering). Spec:
`docs/superpowers/specs/2026-07-14-timeline-group-coverage-design.md`.
- New pure `web/lib/timeline.ts` `buildTimeline(rounds, news)`: clusters each
  news article UNDER the funding round it covers (nearest `announced_date` within
  ±14d; ties → larger amount). A round's `primary_news_url` folds in (deduped by
  canonical URL, primary leads). Read-time only — no migration, no pipeline change.
- `EventTimeline`: a round with ≥2 sources → a collapsed native `<details>`
  "Covered by {distinct outlets} +N more sources" (zero client JS, keyboard/AT
  friendly, `group-open` chevron); ≤1 source keeps the inline ↗. Trust-preserving:
  every article one click away, never dropped; multi-outlet coverage becomes a
  positive "widely covered" signal. Also fixed the old double-render of a round's
  own article.
- Consolidated the http(s) host parse into `web/lib/url.ts` `httpHost` (was 3
  near-identical copies across SourceLink/Sources/timeline — the drift smell a #193
  nit flagged).
- **Adversarial review (3 dims → verify, + a focused 2nd pass): 6 confirmed, all
  fixed.** The load-bearing ones: **pin each round's OWN primary article to that
  round before nearest-date clustering** — else a bridge + a Series A within 14
  days cross-attribute each other's press (nearest-wins), and a null-dated primary
  double-renders; **filter unrenderable URLs up front** so a bad-URL article is
  dropped symmetrically (never coverage-only, never a dead standalone link);
  **name distinct outlets** in the summary (no "techcrunch.com, techcrunch.com").
  Verified: lint + test (**393 passed**) + build; 2nd review pass APPROVE (0 new
  functional defects).
- **Gotcha:** `getCompanyBySlug` does NOT filter null `published_date` (unlike the
  site-wide news queries), so null-dated rows reach `buildTimeline` — the primary
  pinning (by canonical URL, date-independent) is what keeps them from
  double-rendering.
- **Follow-up (BACKLOG):** if the date-proximity mapping proves accurate on the
  real build, persist a `news_articles.funding_round_id` link (pipeline
  classification) for exact grouping; the a11y token/focus-ring pass now also
  covers the disclosure.

## PR #195 — a11y: lift de-emphasized text to WCAG AA contrast (merged 2026-07-14)

The system-wide a11y pass the #193/#194 reviews flagged (owner-requested).
De-emphasized text tokens were below WCAG AA (4.5:1) for text.
- **Lifted `--ink-muted`** to AA in both modes (light #8a8a8a→#6d6d6d = 4.96:1 on
  the #fafafa canvas; dark #5f5f5f→#808080 = 5.01:1 on #0a0a0a) — one token change
  fixing every readable `text-ink-muted` site (host links, source tags, timeline
  coverage, secondary meta) at once. Darkening (light) / lightening (dark) only
  IMPROVES contrast, so no regression.
- **Reclassified 31 readable `text-ink-faint` → `text-ink-muted`** (captions,
  footers, meta, "+N more", "#rank", chart labels), leaving **15 WCAG-exempt**
  untouched: `aria-hidden` decoration, disabled pagination (`cursor-default`), and
  `—` empty-value placeholders.
- **Normalized disclosure focus rings:** added `summary` to the global
  `:focus-visible` outline rule and dropped the 40%-opacity custom
  `focus-visible:ring-accent/40` from `EventTimeline`/`FilterPanel`, so
  disclosures get the site-standard 2px accent outline.
- `--ink-faint`'s value is deliberately left (now only on decorative/disabled/`—`
  uses); the brand `--accent` as link text (~4.36:1, marginally under) is a
  separate deferred token decision (BACKLOG). Verified: lint + 393 tests + build;
  the diff was reviewed line-by-line (all 15 KEEPs confirmed untouched, no
  border/`--edge`/`--accent` change). **A visual change tests can't verify — the
  owner reviewed the Vercel preview and approved before merge.**

## PR #196 — mobile masthead menu + news.google.com "News" label (merged 2026-07-14)

Two web fixes surfaced by a live QA pass of the provenance/trust features on
prod (Perplexity/Norm/Wave/Milestone + a ~350-company same-origin scan).
- **NAV-1 — mobile nav overflow.** The primary masthead nav rendered all eight
  links at every width with no collapse, so on phones the row overflowed the
  viewport and the whole page scrolled horizontally (~90px at 570px, worse at
  375px). Extracted the links to a shared `PRIMARY_NAV` (`lib/nav.ts`) used by
  both the desktop nav (now `hidden lg:block`) and a new `MobileNav` client
  island (`lg:hidden`) — an accessible `☰` dropdown (`aria-expanded`/
  `aria-controls`) that closes on link click, Escape, and outside click.
  Verified live at 375px: horizontal overflow 90px→**0px**; desktop unchanged.
- **LABEL-1 — `news.google.com` was untagged.** Funding rounds cite their
  `primary_news_url` (an `ingest-news` Google News RSS link), so `news.google.com`
  is the host behind most fact citations, but it was absent from `NEWS_HOSTS` →
  those rows rendered with no source-type tag (only the "Website" self-citation
  was labelled). Added the exact host (not bare `google.com`); Google News only
  indexes news, so "News" is never a mislabel.
- Verified: lint + 398 tests (4 new: news.google.com labelling + MobileNav
  behaviour) + `next build --webpack` + `check:bundle`, plus live-browser E2E of
  the mobile menu.
- **Not in this PR (same QA pass, logged to BACKLOG):** the completeness badge
  renders nowhere — NOT a bug (`ProvenancePanel` is correct; prod
  `completeness_score` is just unpopulated → run `compute-completeness`), and
  coverage-grouping-on-undated-rounds needs a `news_articles.funding_round_id`
  link (a pipeline/migration change).
