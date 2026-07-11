# Fable 5 coding-improvement plan — nous

**Date:** 2026-07-10
**Executor:** Claude Fable 5 (as a coding agent — NOT as the runtime enrichment model)
**Scope:** Six workstreams. DeepSeek stays the runtime LLM for all enrichment/extraction;
Fable 5 only writes and hardens the Python/TypeScript around it.

---

## Operating principles (apply to every workstream)

- **Branch + PR per workstream.** Never push to `main` (per `CLAUDE.md`). Each workstream
  lands as one or more reviewed, CI-green PRs on a `fable5/<workstream>` branch.
- **The gate is `lint.yml`.** Before any PR: `ruff check .`, `mypy src`, `alembic upgrade head`,
  `pytest` in `pipeline/`; `npm run lint` + `npm run build` in `web/`. All must pass.
- **Free-tier + idempotency are non-negotiable.** No change may introduce a paid dependency
  or a non-idempotent stage. Flag any cost before implementing.
- **Every rendered fact keeps a source.** No change may render an unattributed number.
- **Verify by exercising, not by asserting.** Drive the real flow (a stage run against a
  local Postgres, a page render) and observe behavior before claiming done.
- **Keep authoring and review separate.** A different pass (code-reviewer / verifier) approves
  each change; no self-approval in the same context.

Suggested sequencing (dependencies): **W-A (test scaffolding)** and **W-E.1 (LLM eval harness)**
are foundations — do them first because W-C (bug fixes) and W-F (description rewrite) are far
safer once they exist. W-B, W-D are independent and can run in parallel.

---

## W-A — Web test suite (highest-leverage gap)

**Goal:** the web app currently has ~zero tests (one Playwright smoke); `npm run build` only
typechecks, so every render-time bug ships unguarded. Give `web/` a real safety net.

**Tasks**
1. Add **Vitest + React Testing Library** to `web/` (jsdom env, `test` script, CI wiring in
   `lint.yml`'s `web` job).
2. **Unit tests for the pure logic** already written "so it stays testable": `lib/spotlight.ts`
   (date-seeded pick determinism), `lib/format.ts` (`formatUsd` rounding + exact-dollar title),
   `lib/compare.ts` / watchlist / saved-search stores (localStorage discipline: snapshot caching,
   tampered-payload tolerance, `MAX_COMPARE=4` cap).
3. **Query-layer tests for `lib/queries.ts`** against a mocked Supabase client — cover the
   PGRST103 out-of-range clamp, the `CATALOG_BAR_OR` filter, `sanitizeIlikeTerm` injection guard,
   the two-hop `getAlsoBackedBy` union + high-degree exclusion, and the excluded-company
   null-out on every surfacing path.
4. **Component render tests** for the risky server components: `Competitors` (META_LEAK regex
   drop), `FundingHistory` (low-confidence pill, exact-dollar title), `Investors` (lead-first
   dedup), `Sources` (citation dedup), `StatusBadge`, husk placeholder.
5. **Expand the Playwright smoke** into a small journey: browse → filter → company page →
   compare → CSV export headers. Keep the secret-free structural block as the CI contract;
   gate data-backed assertions on `SMOKE_HAS_DATA=1`.

**Files:** `web/package.json`, `web/vitest.config.ts` (new), `web/**/*.test.ts(x)` (new),
`web/e2e/smoke.spec.ts`, `.github/workflows/lint.yml`.

**Verify:** `npm run test` (new) + `npm run test:e2e` green locally and in CI; deliberately
break a component and confirm a test catches it.

**Risk:** RSC/`async` component testing is fiddly — prefer testing the data-shaping functions and
extracted presentational pieces over full RSC trees where the harness fights you.

**Effort:** M–L.

---

## W-B — Secret-leak prevention

**Goal:** the runtime posture is already good (service-role key is server-only, SSRF hardened,
RLS-with-no-policies). Close the *process* gap so a future edit can't leak a secret.

**Tasks**
1. **Secret-scanning CI gate** — add `gitleaks` (or trufflehog) as a job in `lint.yml`, failing
   the build on a detected secret. Add a matching pre-commit hook config (documented, opt-in).
2. **Client-bundle safety test** — a test that builds `web/` and asserts the client JS bundle
   never contains `SUPABASE_SERVICE_ROLE_KEY` (or the DB URL). This catches the single most
   catastrophic mistake for this project — a service-role key reaching the browser.
3. **Server-only boundary audit** — assert (lint rule or test) that `lib/queries.ts` / `lib/db.ts`
   are never imported from a `"use client"` module; document the boundary in `web/AGENTS.md`.
4. **`.env` hygiene pass** — confirm `.gitignore` covers every `.env*` variant, both `.env.example`
   files carry only placeholders, and no secret is interpolated into a URL/query string anywhere.

**Files:** `.github/workflows/lint.yml`, `.gitleaks.toml` (new), `.pre-commit-config.yaml` (new),
`web/**/*.test.ts` (new bundle test), `web/AGENTS.md`.

**Verify:** plant a fake key in a client file, confirm the bundle test + gitleaks both fail;
remove it, confirm green.

**Risk:** minimal. Keep gitleaks' allowlist tight to avoid false negatives.

**Effort:** S.

---

## W-C — Bug & discrepancy sweep

**Goal:** fix the concrete defects and drift the codebase review surfaced. Fable 5's
adversarial code-review strength is the reason to use it here.

**Tasks**
1. **Throttle-cooperation bug (verified real).** `HomepageClient` and `HeadlessBrowserClient`
   keep separate per-domain lock dicts, so the browser fallback double-hits a host despite the
   docstring claiming they cooperate on 1 req/s/domain. Fix by sharing throttle state (ties into
   W-E.2's `ThrottledHTTPClient`). Add a regression test proving two transports on one host serialize.
2. **Missing-Supabase-env → silent 404.** `lib/queries.ts` swallows the "not configured" error and
   returns empty, so a prod misconfig 404s every page instead of erroring loudly. Distinguish
   "env missing" (throw/500 at the boundary) from "no row" (legit null) — without breaking the
   secret-free CI smoke contract (guard behind a `NODE_ENV`/build-phase check).
3. **De-duplicate drift risks.** Extract the LLM META_LEAK regex into one shared constant used by
   both `Competitors.tsx` and `queries.ts` `getAlternatives` (currently "kept in sync" by comment).
   Merge the two overlapping aggregator blocklists (`reject_hosts.AGGREGATOR_HOSTS` vs
   `duckduckgo.AGGREGATOR_DOMAINS`) into one source of truth. Fold `extract_funding._IMAGE_HOSTS`
   into `reject_hosts` (there's a TODO).
4. **Total-raised double-count assumption.** Document the `max(stated, sum-deduped-on-(type,amount))`
   invariant near the sum, and add a test for the Helion-style duplicate-round case.
5. **Stale docs discrepancy.** `CLAUDE.md`, `nous-technical-spec.md`, and the older plan/spec docs
   still describe Form D as the spine and Gemini as the LLM — both long gone. Update `CLAUDE.md`
   and add a spec "current state" banner (don't rewrite history in the dated plan docs; annotate).
6. **Non-US backfill drain (still open).** Wire the existing `infer-hq-country` +
   `judge-eligibility --rejudge-nonstartup-signals` as a bounded one-time sweep over existing
   foreign rows (Mistral/Clio/etc.) — a runbook + a gated workflow_dispatch step, not a code change
   to the stages.

**Files:** `pipeline/src/nous/sources/{homepage,headless_browser,reject_hosts,duckduckgo}.py`,
`pipeline/src/nous/pipeline/extract_funding.py`, `web/lib/queries.ts`, `web/components/Competitors.tsx`,
`web/app/c/[slug]/page.tsx`, `CLAUDE.md`, `nous-technical-spec.md`, tests throughout.

**Verify:** regression test per fix; run the affected stage/page locally and observe corrected behavior.

**Risk:** the silent-404 change must not break the CI smoke contract — that's the trap; test both modes.

**Effort:** M.

---

## W-D — Discovery expansion & adapter resilience ("more companies, automatically")

**Goal:** add companies faster *and* stop existing sources from silently dying.

**Tasks**
1. **New discovery adapters** (each a self-contained `sources/` module feeding `auto_create_company`,
   per the backlog): VentureBeat RSS, GeekWire RSS, and one structured accelerator/demo-day list.
   Optionally a GitHub-trending → company mapper (catches devtools TechCrunch misses). Wire each into
   the existing news/portfolio path + `adapter-health` floors.
2. **Adapter resilience framework.** The 13 VC scrapers silently degrade to `[]` on a site redesign
   (CSS/JSON-island coupling). Give the base adapter a "hard-fail on structural miss" contract so a
   zero-yield fetch *raises* rather than returns empty (several already do — make it uniform), and
   ensure every adapter is covered by `adapter-health` with a sensible per-firm floor.
3. **Adapter canary tests** (backlog item): a test per adapter that parses a checked-in fixture of
   the real page and asserts ≥ N entries with well-formed fields — so a layout change breaks CI, not prod.
4. **Extract the balanced-delimiter walker** duplicated across the a16z / Founders Fund / Felicis
   JSON-island adapters into one shared helper.

**Files:** `pipeline/src/nous/sources/*.py` (new adapters + base), `pipeline/src/nous/sources/vc_portfolios/*`,
`pipeline/src/nous/pipeline/{ingest_news,adapter_health}.py`, `pipeline/tests/fixtures/**` (new),
`.github/workflows/{pipeline,discovery}.yml` (register new adapters).

**Verify:** run each new adapter against its live source (or fixture) and confirm N entries;
`adapter-health` reports each; canary tests fail when a fixture is mangled.

**Risk:** live scraping is fragile — pin canary tests to checked-in fixtures, not the network.
Respect robots.txt + the 1 req/s throttle for any new source.

**Effort:** M (S per adapter).

---

## W-E — Backend infrastructure

**Goal:** the plumbing that makes everything else safer and un-blocks W-F.

**Tasks**
1. **LLM eval golden set + harness (foundation for W-F and all prompt edits).** ~20 hand-checked
   fixtures per prompt (article/page → expected extraction), run offline in CI on every prompt
   change. Prompts currently "ship blind." Structure it so a prompt edit reports precision/recall
   deltas against the golden set. Uses recorded fixtures, not live DeepSeek calls (free, deterministic).
2. **Prompt versioning.** Stamp a `prompt_version` on every extraction row (migration + column +
   thread through the writing stages) so data produced by a bad prompt revision can be selectively
   re-run. Small migration, high operational value.
3. **`ThrottledHTTPClient` refactor.** Extract the triplicated throttle/GET/tenacity logic from
   `homepage.py` / `news.py` / `headless_browser.py` into `sources/_http.py` with a **shared**
   per-domain registry (this is also the fix for W-C.1). Migrate all three clients onto it.
4. **`slug_aliases` table + 301 redirects.** Dedup merges currently DELETE loser rows, burning
   inbound links / SEO equity. Record old-slug → new-slug at merge time in `dedup_companies.py`;
   add `web/` middleware that 301s an aliased slug to the survivor. Migration + middleware + merge wiring.

**Files:** `pipeline/tests/golden/**` (new), `pipeline/src/nous/llm/**`, `pipeline/alembic/versions/*`
(new migrations — hand-written, per project convention), `pipeline/src/nous/db/{models,upsert}.py`,
`pipeline/src/nous/sources/{_http,homepage,news,headless_browser}.py`,
`pipeline/src/nous/pipeline/dedup_companies.py`, `web/middleware.ts` (new).

**Verify:** eval harness runs in CI and catches a deliberately-degraded prompt; `ThrottledHTTPClient`
regression test proves cross-transport serialization; merge two companies and confirm the old slug 301s.

**Risk:** any new migration must be **hand-written** — `--autogenerate` drops the trigram/partial/unique
indexes it can't model (load-bearing, repeated in every migration docstring from 0015 on).

**Effort:** M–L.

---

## W-F — Richer company descriptions (prompt quality)

**Goal:** descriptions read too short and thin. The single enrichment prompt is dominated by
classification; the `description_long` instruction is ~2 lines requesting only what/who/how/distinctive,
with no depth floor, so DeepSeek writes 3 short paragraphs and stops. Make profiles "something you'd
actually enjoy reading" — **without increasing hallucination.** Runtime stays DeepSeek.

**Tasks**
1. **Rewrite the `description_long` contract** in `company_description.py`: request more depth and
   explicit dimensions — the problem being solved, how the product works (incl. technical approach
   when stated), who it's for and the use cases, business model, market/competitive context, the
   founding wedge / what's distinctive, and notable customers or traction *only when stated*. Set a
   real target (e.g. ~350–600 words / 4–7 paragraphs) with a floor, keep the "say so plainly if the
   site is thin — never invent" guard front-and-center.
2. **Consider splitting describe from judge.** The one call does six jobs; optionally move the richer
   description into its own prompt/pass so length isn't crowded out by the eligibility/classification
   instructions. Weigh the extra DeepSeek call against quality (flag the cost).
3. **Secondary input lever:** raise the scraped-subpage count / per-company char budget modestly so
   content-rich sites feed more source text (bounded — more input = more DeepSeek cost/latency).
4. **Validate with the W-E.1 golden set:** prove the rewrite lengthens + enriches descriptions on
   real fixtures while *not* adding unsupported claims (spot-check a hallucination rubric). Ship
   behind `prompt_version` (W-E.2) so a targeted re-enrichment can regenerate the backlog.

**Files:** `pipeline/src/nous/llm/prompts/company_description.py`, possibly a new
`company_description_long.py`, `pipeline/src/nous/pipeline/enrich_companies.py`,
`pipeline/src/nous/sources/scrape_homepages.py` (subpage count), golden fixtures.

**Verify:** run `enrich-companies --limit N --refetch-after-days 0` against a local DB seeded with a
few real companies; read the before/after descriptions; confirm length + depth up, no invented facts;
golden-set metrics hold.

**Cost note:** ~150→~450 words ≈ 3× the description's *output* tokens at $1.10/Mtok. Write-once per
company (gated on `description_short IS NULL`), so mostly a one-time backlog re-run cost — modest,
but flag the total before a full re-enrichment.

**Effort:** M (depends on W-E.1 existing).

---

## Rollout order (recommended)

1. **W-A** web test scaffolding + **W-E.1** eval harness + **W-E.3** `ThrottledHTTPClient` (foundations).
2. **W-B** secret-leak prevention (small, high-stakes, independent).
3. **W-C** bug & discrepancy sweep (now safe — tests + eval exist; W-C.1 rides W-E.3).
4. **W-F** richer descriptions (rides W-E.1 + W-E.2).
5. **W-D** discovery expansion + resilience.
6. **W-E.4** slug aliases + **W-E.2** prompt versioning (whenever; W-E.2 before W-F ships to prod).

Each numbered item is one or more PRs. Nothing here touches the DeepSeek-vs-anything runtime choice.
