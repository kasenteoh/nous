# Backlog

> **Strategic layer:** [`ROADMAP.md`](ROADMAP.md) holds the *why / what order*
> (Now / Next / Later bets); this file is the tactical *what next* queue. A
> roadmap bet becoming concrete work lands here as an entry.

> **2026-07-12 status sweep:** the `fable5/*` series (PRs #131–#155, see
> `docs/superpowers/fable5-worklog.md`) shipped large parts of this backlog:
> all P2 pipeline cleanups, the frontend fixes, slug aliases + 301s (Wave 2),
> and the Wave 3 embeddings stack (embeddings infra, similar-companies,
> semantic search, themes). Entries below are annotated SHIPPED where done;
> unannotated entries remain open.

The grind queue. Refreshed 2026-06-11 after a full codebase review + product
brainstorm: items shipped in PRs #23 and #28–31 were closed (the M5 P1 fixes,
index search/filters/pagination, `/about`, employee rendering, low-confidence
funding flags), and the product backlog below was added. Add new entries at the
bottom of the appropriate section; close items by deleting them.

**Severity / effort legend:**
- **P0** — correctness or cost risk; do before new features
- **P1** — should fix soon; **P2** — fix opportunistically
- **[S]** hours · **[M]** days · **[L]** a week or more

---

## 2026-06-16 product review + remediation — SHIPPED

Review: [2026-06-16-product-review-and-next-steps.md](docs/superpowers/plans/2026-06-16-product-review-and-next-steps.md).
Execution log + activation steps: [2026-06-16-remediation-execution-log.md](docs/superpowers/plans/2026-06-16-remediation-execution-log.md).

Every bug the four-persona review found, plus the high-value backlog items below,
shipped as PRs #112–#128 (verified on prod):

- ✅ Husk notice + `discovered_via` label (#112) · marquee-husk enrichment
  prioritisation (#114) · wrong-company profile detect + resolver hardening (#117)
  · funding sources → publisher URLs (#118) · phantom valuation rounds (#124)
- ✅ Eligibility rejects non-startups + opt-in re-judge (#115) · news
  mis-attribution guard (#116)
- ✅ Investor dedup a16z/junk/angels (#113) · compare selection UI (#119) ·
  investor pagination + profile (#120) · amount tooltips + attribution (#121)
- ✅ Company logos (#122/#125/#126) · name-quality casing (#123) · state-display
  normalization (#125) · Alternatives pages + FAQ JSON-LD (#126)
- ✅ Adapter-health canary (#127) · filter-column indexes / migration 0030 (#128)
- ✅ Stale `repoIssueUrl` comment removed (#112-era)

**Pending activation** (one-time prod dispatches + workflow wiring — see the
execution log's "Activation" section): run `repair-wrong-websites` /
`repair-duplicate-rounds` over existing rows; `judge-eligibility
--rejudge-nonstartup-signals` for the Manta/Lucra leaks; wire `name-quality` +
`adapter-health` into `discovery.yml`. The every-3h cron heals going-forward data
automatically.

**Still open from the review:** news-list de-dup/ranking (E2); a deliberate non-US
backfill drain (V2 — eligibility now rejects on entry, but existing foreign rows
like Mistral/Clio need a sweep); mobile-responsiveness pass (P3).

---

## 2026-07-13 ROADMAP "Now" horizon — data-quality foundation

Strategic context: [ROADMAP.md](ROADMAP.md) (Now horizon). Earn the right to be
trusted before building depth. Sequence: measure quality → fix the biggest hole
(husks) by re-mining not re-scraping → make correctness visible. New items are
detailed below; existing open entries pulled into this push are cross-referenced
at the end.

### Resolve husk websites by re-mining, not re-scraping [M] — P1 — SHIPPED (#172/#173/#174)
New idempotent `resolve-website-fallback` stage resolves website-less husks from
non-origin sources, first accepted candidate wins, `$0`, self-bounding on
`website IS NULL` + its own `website_fallback_checked_at` stamp, wired into the
3h cron before resolve-homepages (drains ~25/run). Provenance recorded per
resolved site (`website_source` + `website_source_url`, migration 0037).
- **wikidata** — Wikidata "official website" (P856) for a name + org-type +
  country matched entity (three precision gates; a conservative country
  cross-check rejects confirmed-foreign same-name collisions). **Highest yield +
  precision.**
- **news_outbound** — the company's homepage link in an already-sourced news
  article body, re-fetching the *article* (not the origin) and matching by
  domain-label / anchor name.
- **Dry run (30 prominent husks):** 11 resolved (37%), 0 conflicts, ~10/11
  correct, `$0`. wikidata 9, news_outbound 2 (disjoint).
- **Not built:** VC-portfolio source (the roadmap assumed `raw_pages` caches
  portfolio pages — it doesn't; it's company-scoped, and portfolio adapters
  already capture `entry.website` at discovery, so it's redundant for
  portfolio-discovered husks). Common Crawl (weak for name→domain). Revisit only
  if the dashboard shows the residual husk count stays high.
- **Follow-up:** the faster-backfill lever (`resolve-website-fallback.yml`
  dispatch, `dry_run=false`) can drain the ~890 backlog quicker than 25/run if
  the gradual cron drain proves too slow.

### Data-quality dashboard [M] — P1 — SHIPPED (#175)
Read-only `data-quality` stage (completeness sibling of db-stats/pipeline-health)
emits a step-summary report over the shown cohort: field-completeness %s
(website / description / funding / logo / people / location / industry / tags /
employees), **website provenance by `website_source`** (surfaces the #174
re-mining contribution + wrong-site proxy), completeness-score distribution,
duplicate rate (shared `normalized_name`), enrichment staleness. Id-free cron
step. **Follow-up:** a web-facing version is ROADMAP Later (provenance UI); this
is internal-report-only for now.

### Per-company completeness / confidence score [S] — P2 — SHIPPED (internal primitive, #175)
Pure `util.completeness` weighted 0..1 score (husk-defining fields dominate),
aggregated by the data-quality report. **Remaining:** wire it into
husk-enrichment prioritisation ordering, and a public trust badge (ROADMAP Later
— provenance UI). `extraction_confidence` not yet folded in (field-presence
only for now).

### Pulled into this push — existing open entries
Consciously scoped into the Now horizon; tracked in their home sections, listed
here so the push is complete:
- **"Report incorrect data" link** (Wave 1) — **SHIPPED (#177)**: per-company
  `repoIssueUrl` rider restored on `web/app/c/[slug]/page.tsx` (repo public → the
  prefilled GitHub-issue link resolves).
- **`formatUsd` rounding collapses distinct amounts** — **SHIPPED (#177)**:
  `title={formatUsdExact(amount)}` on every individual funding figure.
- **`hq_state` unnormalized (CA vs California)** — **SHIPPED (#176)** —
  canonicalized to the 2-letter USPS code at enrichment write-time + a
  `normalize-hq-state` backfill.
- **Tag min-companies threshold** — **SHIPPED (#177)**: `/tag/[tag]` noindex when
  <3 companies, in lockstep with the sitemap's existing ≥3 filter.

---

## Pipeline cleanups (P2)

### Throttle/get helper triplicated across source clients [M]
**SHIPPED — PR #132.**
[homepage.py](pipeline/src/nous/sources/homepage.py),
[news.py](pipeline/src/nous/sources/news.py), and
[headless_browser.py](pipeline/src/nous/sources/headless_browser.py) each
reimplement domain locks + throttled GET + tenacity. They also keep separate
lock dicts, so HomepageClient and HeadlessBrowserClient do **not** actually
cooperate on the 1 req/sec/domain budget despite the comment claiming they do.
Extract a `ThrottledHTTPClient` in `sources/_http.py` with a shared registry.

### Add btree index on `companies.hq_state`
**SHIPPED — PR #128 (pre-series).** and GIN on `companies.tags` (now in `WHERE` via /location and /tag pages); batch with other unindexed filter columns (`industry_group`, `discovered_via`). [S]

---

## Frontend fixes (P2)

### Description-source attribution is misleading [S]
**SHIPPED — PR #121 (pre-series).**
[c/[slug]/page.tsx](web/app/c/%5Bslug%5D/page.tsx) says "generated by … from
[hostname]" even when the description was derived from multiple pages. Soften
to "Generated on [date]" or track per-description sources.

### Missing Supabase env collapses into 404 [S]
**SHIPPED — PR #138.**
[queries.ts](web/lib/queries.ts) returns `null`/empty indistinguishably for
"missing env" vs "no row", so a misconfigured deployment 404s every page.
Throw at module load (server-only) so misconfigs fail fast and loud.

### Total-raised tile may double-count overlapping rounds [S]
**SHIPPED — PR #138.**
The detail page sums `amount_raised` across all rounds; if
`reconcile_funding_round` ever fails to merge two articles about the same round,
the tile double-counts. Document the assumption near the sum; longer-term add a
`round_correction_of` pointer for amended rounds. Since the hybrid total-raised
change, an article-stated cumulative total caps the displayed figure whenever
articles state one that exceeds the sum (the tile shows max(stated, sum) —
partial mitigation); the reconcile-merge risk itself stands.

### ~~`formatUsd` rounding collapses distinct amounts~~ [S] — SHIPPED (#177)
$1.51M and $1.49M both rendered as "$1.5M"; now every individual funding figure
carries a `title={formatUsdExact(amount)}` exact-dollars tooltip.

### ~~`hq_state` values are unnormalized (CA vs California) — location pages render stored casing; normalize at enrichment time.~~ [S] SHIPPED (#176)
Canonical form = the 2-letter UPPERCASE USPS code (the form the `/location/[state]` route already matches on — routing-safe). Applied at the enrich-companies write site via `canonical_us_state` (`util/us_state.py`, 50 states + DC; non-US → None → left untouched) plus the bounded, idempotent `normalize-hq-state` backfill stage (`--limit` / `--dry-run`, self-bounding SELECT, per-row commit). No migration (content-only), no URL change (full-name `/location/California` links 404 today and start resolving to the working `/location/CA`).

### Tag sitemap min-companies threshold [S] — **partly SHIPPED (#177)**
Thin single-company tag pages: `/tag/[tag]` now `noindex` when <3 companies, and
`sitemap.ts` already excludes tags with <3 (`listAllTags`). **Still open:** a
sitemap *index* before companies+tags approach the 50k-URL sitemap cap.

---

## Product backlog — Wave 1: free wins

All buildable from data already in the DB; mostly frontend.

### "Report incorrect data" link [S]
Prefilled GitHub-issue URL on every company page. Crowdsourced QA, zero backend.
Built in PR (feat/seo-pack) but rendering deferred — repo is private so the
issues URL 404s for visitors. Re-enable the rider in web/app/c/[slug]/page.tsx
when the repo goes public (or swap target to a public form/mailto).

### Name-quality pass [S]
Prefer the company's own `og:site_name` / `<title>` casing (already in
`raw_pages`) over VC-portfolio casing. Folds in the old `name_quality`
source-priority idea: rank sources, overwrite only on higher quality.

### Logos via favicon fetch [S]
`companies.logo_url` exists and is mostly unused. Fetch
`/favicon.ico`/`apple-touch-icon` during scrape-homepages; render on cards and
detail header.

---

## Product backlog — Wave 2: the relationship graph (differentiator)

Build order matters: each step makes the next cheaper. Full design notes in the
2026-06-11 review.

### Fuzzy competitor linking [S–M]
[analyze_competitors.py](pipeline/src/nous/pipeline/analyze_competitors.py)
resolves competitor names by exact `normalized_name` only, leaving many edges
dangling. New `link-competitors` stage: pg_trgm `func.similarity` (the pattern
already in [dedup_companies.py](pipeline/src/nous/pipeline/dedup_companies.py))
≥ threshold, best-match-only with a tie guard, only touches NULL FKs. Zero LLM
cost; instantly densifies the graph. Call the same helper from
analyze_competitors at write time going forward.

### `company_relationships` edge table + derive stage [M]
Typed edges: `competitor | partner | vendor_of`, with `counterpart_name`,
`source`, `source_url`, evidence quote, confidence; unresolved counterparts kept
by name; unique on `(company_a_id, normalized_counterpart_name, rel_type,
source)`; canonical a<b ordering for symmetric types. Keep `competitors` as-is
(ranked per-company artifact with a UI contract) and project resolved pairs into
the edge table via a set-based `derive-relationships` stage (replace-style,
zero LLM). Do **not** materialize shared-investor edges (O(N²) with YC-scale
portfolios) — derive those at read time, capped.

### Related-companies module on `/c/[slug]` [M]
Server-rendered section grouping edges by type ("competes with", "works with")
with evidence/source links, plus an "also backed by" fallback via a two-hop
`company_investors` query (exclude investors with >30 holdings). First
user-visible payoff of the graph.

### "Alternatives to X" pages [M]
Generated from competitor edges. Huge search volume; makes the graph data earn
traffic before any visualization exists.

### "X vs Y" compare pages [M]
Competitor pairs define the URL space; render two profiles side by side.

### LLM partner/supply-chain extraction — dry-run first [M]
**Risk gate:** before building plumbing, run the extraction prompt over ~20
companies' existing articles/pages (~$0.50) and inspect yield + hallucination
rate. Funding news rarely names vendors and customer logos are images, so this
edge type may be sparse. If yield is good: `extract-relationships` stage over
already-cached `news_articles` + `raw_pages`, new prompt under `llm/prompts/`,
capped ~100 articles/run (~$0.15/wk), weekly cron in the shared concurrency
group. If poor: competitor edges + themes carry the map; drop the type.

### Market map — `/map/[industry]` [L] — SHIPPED (#179 pipeline, #180 web)
**Pipeline side SHIPPED (#179)** — `compute-map-positions` stage: per-industry
scikit-learn PCA(2) over the shown+embedded description embeddings (E-1),
deterministic sign-pin + per-axis min-max to `[0,1]²`, written to three new
nullable columns on `companies` (`map_x`, `map_y`, `map_computed_at`; migration
0038). Coords are comparable only *within* an `industry_group` (own PCA basis).
`$0` — local CPU PCA, no LLM, no network; reuses the `embeddings` uv group;
per-industry TTL-gated (25d) off weekly `discovery.yml` → effective monthly.
The web read is a flat single-table `WHERE industry_group = $1 AND map_x IS NOT
NULL` (no RPC, no PCA on Vercel — the #157 lesson).

**Web side SHIPPED (#180)** — shipped as a **static server-rendered SVG** (no
client component, no ML on the web function — the #157 lesson): `/map/[industry]`
reads `map_x`/`map_y` and renders nodes (SVG `<a>` links, funding-sized radius,
greedy non-overlapping labels, a11y via `aria-labelledby` + `sr-only` fallback)
plus a `/map` hub, both canonical-gated + coords-gated in the sitemap.
Migration-ordering-for-free: the queries degrade to an empty-state until coords
land. **Follow-ups (deferred):** an interactive client renderer (d3-force /
`react-force-graph-2d`) + theme coloring + a global theme-level meta-graph; one
visual tuning call (per-axis min-max exaggerates the lower-variance PC2 — switch
to a single shared scale factor to preserve the true PC1:PC2 ratio).

### `slug_aliases` table with 301 redirects [M]
**SHIPPED — PR #141 (308 miss-path redirects).**
Promoted from Future: dedup merges actively delete loser rows today, burning
inbound links and SEO equity. Keep old slug → 301 → new slug; middleware in
`web/` reads the table. Record aliases at merge time in
[dedup_companies.py](pipeline/src/nous/pipeline/dedup_companies.py).

---

## Product backlog — Wave 3: intelligence ("what's evolving")

### Embeddings infrastructure [M]
**SHIPPED — PR #153.**
pgvector (free on Supabase; `CREATE EXTENSION vector` in a migration) +
`companies.embedding vector(384)`. Generate with fastembed
(`BAAI/bge-small-en-v1.5`, ONNX, CPU) inside GitHub Actions — $0, seconds per
run; optional uv dependency group so the main install stays light; cache the
model dir. ~8MB storage at 5k companies; exact scan is fine, no index needed.

### Semantic search [M]
**SHIPPED — PR #155.**
"Startups doing AI for logistics" — embed the query, nearest-neighbor over
company embeddings, blend with the existing ilike search on the index page.

### Themes pipeline + pages [L]
**SHIPPED — PR #154.**
Monthly `compute-themes` stage: cluster embeddings within each `industry_group`
(KMeans/HDBSCAN), one DeepSeek call per cluster to name it (~50–100 calls =
pennies) → `themes` + `company_themes` tables (replace-style per industry;
centroid-match to previous run at cosine ≥0.9 to keep slugs stable-ish).
`/themes/[slug]`: member companies, funding-by-quarter (server-rendered SVG
bars from `funding_rounds.announced_date`), new entrants. `/themes` index
ranked by trailing-2-quarter funding growth — the literal "what's heating up"
page.

### Industry pages — `/industry/[group]` [M]
Company count, 12-mo funding velocity, median round, most active investors,
newest entrants, market-map embed.

### Trends dashboard — `/trends` [M]
Funding by industry over time, rising tags, heating/cooling indicators. All
derivable from `announced_date` + `created_at`.

### Similar-companies module [S]
**SHIPPED — PR #153.**
Nearest neighbors by embedding on every company page ("people also viewed"
without needing analytics). Rides on the embeddings infra.

---

## Product backlog — Wave 4: habit loop & breadth

### Weekly auto-digest page + RSS [M]
LLM writes a short "this week in startups" from the pipeline delta (new
companies, new rounds); published as a page + RSS feed. Keep it
aggregate-grounded — numbers from the DB, prose around them. Email is
deliberately deferred (first true cost item).

### Watchlists via localStorage [M]
"My companies" with new-round badges since last visit. No accounts, no backend.

### Momentum signals [M]
Headcount and job-posting growth charts from `company_snapshots` (Wave 1 started
recording). "Headcount up 40% since January" is the most VC-shaped datapoint we
can add. News-cadence sparkline from `news_articles` dates is free.

### `company_events` unified timeline [L]
Generalize funding extraction into event extraction: funding, acquisition,
launch, leadership change, layoffs — one timeline table, one timeline component
on the company page. Feeds the digest. Builds on the Wave-0 status detection.

### Startup of the day [S]
Deterministic daily pick (hash of date) from enriched companies; shareable.

### Compare view [S]
Side-by-side 2–3 companies (distinct from the SEO-oriented X-vs-Y pages:
user-selected, not pre-generated).

### Funding timeline SVG [S]
Small server-rendered visual above the funding table.

### Tech-stack detection [M]
Parse cached homepage HTML for stack hints (script srcs, meta generators) →
"built with" chips. New extraction over existing `raw_pages`, no new scraping.

### Discovery adapters [S each]
One `sources/` adapter apiece: PRNewswire/BusinessWire RSS (funding hits the
wires before TechCrunch), VentureBeat + GeekWire RSS, GitHub trending →
company mapping (devtools channel TC misses), accelerator demo-day lists.

### AI-answer-engine distribution [M]
`llms.txt`, a markdown endpoint per company (`/c/[slug].md`), FAQ block ("What
does X do? Who founded X? How much has X raised?") with FAQPage JSON-LD.
Getting cited by ChatGPT/Perplexity is the new SEO and our clean sourced data
is exactly what they want.

### `company_aliases` table [M]
Carried from Future: track every name variant seen per company + source.
Recovers from bad name choices with an audit trail; unlocks "you searched
'OpenAI Inc' → here's OpenAI" search behavior.

---

## Ops & quality hardening

### Adapter canary tests [S]
VC portfolio scrapers break silently on site redesigns. Weekly job asserts each
adapter yields > N entries; alert (issue) on collapse. Cheapest insurance
available.

### LLM eval golden set [M]
~20 hand-checked articles → expected extractions, run on every prompt change.
Prompt edits currently ship blind.

### Prompt versioning [S]
Stamp a prompt version on every extraction row so data produced by a bad prompt
revision can be selectively re-run.

### Pipeline observability [M]
`pipeline_runs` table (stage, started/finished, counts, errors); workflow opens
a GitHub issue on failure; public `/stats` freshness page (doubles as a trust
signal for readers).

### Sentry (free tier) for web; Lighthouse CI [S]

### Vitest + one Playwright smoke test for `web/` [M]
Zero web tests today; `npm run build` typechecks but misses render-time bugs.
One happy-path "/c/[slug] renders" test is high-leverage.

---

## Future ideas (need a spec discussion first)

### Human-review admin for dedup candidate pairs
`dedup-companies` auto-merges on exact domain and LLM-gates fuzzy pairs at high
confidence. An admin view surfacing medium-confidence pairs for manual approval
remains a possible enhancement.

### Deliberately deferred — with reasons
- **Accounts/auth** — localStorage watchlists cover the consumer need; auth adds
  email infra, privacy surface, and session bugs for zero differentiation today.
- **Public API** — free-tier egress (5GB/mo) + scraper abuse risk; quarterly
  static JSON/CSV dumps get most of the goodwill at none of the risk.
- **LLM-written narrative reports** ("State of AI Infra") — one hallucinated
  claim damages the trust that is our moat; aggregate-driven pages (themes,
  trends) say the same thing with sourceable numbers.
- **Email digest** — first true cost item (sending infra); RSS + page first.
