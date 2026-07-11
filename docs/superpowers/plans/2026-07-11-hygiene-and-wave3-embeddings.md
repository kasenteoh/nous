# Hygiene wave + Wave 3 (embeddings) plan — nous

**Date:** 2026-07-11
**Executor:** Claude Fable 5 (orchestrator + worktree subagents), continuing the
2026-07-10 improvement-plan conventions: `fable5/*` branches, CI green before
merge (full `statusCheckRollup` verified explicitly — see the red-main incident
in the worklog), squash-merge, hand-written migrations, worklog entry per PR.
DeepSeek stays the runtime LLM. Cost notes flagged inline.

Approved by the user 2026-07-11 ("lets do it") following the post-W-F review.

---

## Part 1 — Hygiene wave (small, parallel)

### H-1: Prominent-husk rescue (highest visible ROI)

Perplexity — one of the most prominent companies in the catalog — renders a
husk page with no description; JS-heavy sites scrape thin and the headless
fallback evidently didn't rescue them. Tasks:

1. Diagnose the husk path end-to-end: when does scrape-homepages fall back to
   Playwright, what does it store for SPA-shell sites, and why did prominent
   companies end up with no `description_short` (thin text? judge said
   insufficient? scrape never re-attempted?).
2. Make scrape-homepages prioritize *shown, description-less* companies by
   prominence and force the headless path for them when the static fetch
   yields under the describe threshold; ensure the refetch back-off doesn't
   permanently bury them.
3. Ensure the enrich selection picks the rescued pages up (it already gates on
   `description_short IS NULL`) and the W-F describe call runs on the fresh
   text.
4. Tests per house pattern; a runbook note (or reuse of existing dispatch
   inputs — pipeline.yml is AT the 25-input cap; prefer behavior over new
   inputs).

**Verify:** dispatched bounded run rescues Perplexity + several other
prominent husks on the live site. **Effort:** S–M.

### H-2: Canonical tag vocabulary

Live evals showed DeepSeek's open tag vocabulary barely overlaps curated
expectations (tags_f1 ~0.35, e.g. `ci-observability` vs `ci-cd`), feeding the
long tail of thin single-company `/tag/*` pages. Tasks:

1. A canonical tag map in `util/` (same pattern as `normalize_industry`):
   alias → canonical, applied at write time in enrich AND as a set-based
   `normalize-taxonomy` pass over existing rows (idempotent, no LLM).
   Seed the map from the actual live tag distribution where observable
   (tag pages/sitemap) + the golden fixtures; keep it extensible.
2. Tighten the judge prompt's tag instruction (prefer established generic
   tags, lowercase-hyphenated, 3–6 tags) — bump its PROMPT_VERSION.
3. Consider a min-companies threshold for tag sitemap URLs (backlog item) if
   it falls out naturally; otherwise leave for later.

**Verify:** normalize pass converges (second run rewrites 0 rows); golden
gate green; tag pages consolidate on the live site after a run.
**Effort:** S–M.

### H-3: Funding-keyword matcher word-boundary fix + GitHub-trending mapper

1. Fix `_matches_funding_keyword` (sources/news.py) substring matching —
   "e**valuation**s" currently matches "valuation". Word-boundary regex over
   the keyword list, tests pinning the false-positive class W-D observed.
   Audit other substring matchers in the news path for the same class.
2. GitHub-trending → company discovery mapper (deferred from W-D): fetch the
   trending repos page (fixture-tested), map org → company candidate, gate
   through an LLM judgment (is this a company with a real product, not a
   personal project?) before auto-create; wire into the weekly discovery
   path with adapter-health visibility. Flag per-run LLM cost (≤25
   candidates/run ≈ pennies).

**Verify:** matcher tests; one-time live parse of trending; canary fixtures.
**Effort:** S + M.

---

## Part 2 — Wave 3: embeddings → similar-companies → semantic search → themes

Sequenced so each PR ships standalone value; descriptions are now W-F-rich,
which is what makes the embeddings worth computing.

### E-1: Embeddings infrastructure + similar-companies module

1. Migration (hand-written): `CREATE EXTENSION vector` + `companies.embedding
   vector(384)` nullable + `companies.embedded_at` + description-hash column
   for idempotent re-embeds. Exact scan (no index) at current scale.
2. `embed-companies` stage: fastembed (`BAAI/bge-small-en-v1.5`, ONNX, CPU) in
   an optional uv dependency group; embeds `name + description_short +
   description_long` for shown companies whose hash changed; bounded
   `--limit`; wired into pipeline.yml after enrich (rides existing cadence,
   no new inputs) with the model dir cached in Actions.
3. Similar-companies module on `/c/[slug]`: nearest-neighbor query (server
   component, service-role path), blended with/replacing the existing
   related-companies heuristics where embeddings exist; renders nothing when
   the company has no embedding (no fabrication).
4. Tests: stage tests (deterministic fake embedder), query tests with the
   mock builder, component test.

**Cost:** $0 LLM; ~8MB storage at 5k companies. **Effort:** M.

### E-2: Semantic search

Embed the query server-side (same model via a small on-demand runner — decide:
fastembed in a route handler is too heavy for Vercel, so run query embedding
through Supabase RPC/pgvector distance on a *pre-embedded query* is not
possible → the pragmatic path is hybrid: keep ilike for instant results and
add a "semantic" mode that hits a lightweight embedding endpoint. Investigate
options (Vercel function size limits vs an Actions-precomputed tag/theme
expansion) BEFORE building; if query-time embedding is infeasible on free
tier, ship theme/tag-expansion search instead and say so in the PR.
**Effort:** M, with an explicit feasibility gate.

### E-3: Themes pipeline + pages

Monthly `compute-themes` stage: cluster embeddings per `industry_group`
(KMeans/HDBSCAN), one DeepSeek call per cluster to name it (~50–100 calls =
pennies), `themes` + `company_themes` tables (replace-per-industry, centroid
match ≥0.9 cosine to keep slugs stable), `/themes` index ranked by
trailing-2-quarter funding growth + `/themes/[slug]` pages with
funding-by-quarter SVG. **Effort:** L — ships after E-1 proves embedding
quality.

Industry pages + `/trends` ride the same data later (not in this plan's
committed scope).

---

## Rollout order

1. H-1, H-2, H-3 in parallel (independent files).
2. E-1 (migration owner — no other migration lands concurrently).
3. E-2 feasibility spike → build or re-scope.
4. E-3.

Every PR: full verification suite; live-site spot-check where user-visible.
