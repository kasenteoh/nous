# Backlog

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

## Now — correctness & cost (P0/P1)

### P0 [M] — `raw_pages` stores full HTML and will exhaust the Supabase 500MB cap
[scrape_homepages.py](pipeline/src/nous/pipeline/scrape_homepages.py) caches the
complete raw HTML of ~4–5 pages per company. At a few thousand companies this is
plausibly the majority of our 500MB free tier, and it grows every weekly run.
The LLM only ever consumes extracted visible text. Fix: store extracted text
(or compressed HTML) in `raw_pages.content`, with a one-time migration shrinking
existing rows. This is the only backlog item with a hard (if hidden) deadline.

### P1 [S] — Wire the Vercel deploy hook after pipeline runs (completes M6)
`VERCEL_DEPLOY_HOOK_URL` exists in [config.py](pipeline/src/nous/config.py) but
nothing calls it. After a pipeline run the site serves up to 6h-stale ISR pages,
and only refreshes pages that get traffic. Fix: final step in each scheduled
workflow POSTs the hook (skip on failure).

### P1 [M] — Company status detection (active / acquired / shut_down / ipo)
VC portfolios list exits, so we currently render acquired and dead companies as
live startups — a correctness problem, not a feature. Add `companies.status`
(+ `status_source_url`), extract from news articles we already ingest (the
funding-extraction pass is already reading them), and badge non-active companies
on the index and detail pages.

### P1 [S] — Dead-site detection flag
Repeated homepage fetch failures across scrape cycles ⇒ mark "possibly
inactive". `last_scrape_attempt_at` already exists; add a consecutive-failure
count and surface a muted flag on the company page.

### P1 [S] — DB-size watchdog + per-stage LLM cost ledger
Log table sizes and LLM call/token counts at the end of each pipeline run; warn
loudly at 80% of the 500MB cap. Half the product backlog below adds LLM calls —
we need the ledger before stacking stages, to keep the ~$1/week DeepSeek budget
honest.

### P1 [S] — Confirm `cleanup-form-d` ran in prod, then delete the stage
[cleanup_form_d.py](pipeline/src/nous/pipeline/cleanup_form_d.py) is a one-time
migration that lives in no cron. If it never ran, legacy rows are still
mis-tagged. Run it (dry-run first), then remove the stage and its tests.

---

## Pipeline cleanups (P2)

### TC-path `auto_create_company` ignores the configured similarity threshold [S]
[ingest_news.py](pipeline/src/nous/pipeline/ingest_news.py) calls without
`similarity_threshold=`, defaulting to 0.85. Today that matches
`Settings.COMPANY_FUZZY_MATCH_THRESHOLD`, but a config tweak silently desyncs
the VC and TC paths. Plumb the setting through like
[refresh_vc_portfolios.py](pipeline/src/nous/pipeline/refresh_vc_portfolios.py) does.

### `find_company_by_name` over-matches short normalized names [S]
[upsert.py](pipeline/src/nous/db/upsert.py): trigram similarity is unstable for
very short strings — "AI", "Vue", "X" can fuzzy-match unrelated companies at
0.85. Add a minimum-length guard (`if len(norm) < 6: return None`) inside the
trigram branch.

### Slug disambiguator still has a non-deterministic fallback [S]
[slugify.py:109](pipeline/src/nous/util/slugify.py): the seeded-sha256 path
shipped, but when no seed is provided it still falls back to
`os.urandom(3).hex()`. Audit callers and pass a seed (name + website) everywhere
so the fallback can be removed.

### Competitor self-reference is not blocked at the DB [S]
[models.py](pipeline/src/nous/db/models.py): nothing prevents
`company_id == competitor_company_id`. Add
`CheckConstraint("competitor_company_id IS NULL OR competitor_company_id != company_id")`
via a migration.

### `competitors.rank` not enforced contiguous 1..N [S]
[analyze_competitors.py](pipeline/src/nous/pipeline/analyze_competitors.py)
trusts the LLM's `rank` as ordinal; sparse ranks (1, 2, 5) render as "Top 3,
then #5". Re-rank the resolved list to 1..N before insert.

### `news_articles.url` indexed twice [S]
[0003_m3_schema.py:89](pipeline/alembic/versions/0003_m3_schema.py): both
`UniqueConstraint("url")` and a redundant unique index. Drop
`ix_news_articles_url` in a migration.

### Throttle/get helper triplicated across source clients [M]
[homepage.py](pipeline/src/nous/sources/homepage.py),
[news.py](pipeline/src/nous/sources/news.py), and
[headless_browser.py](pipeline/src/nous/sources/headless_browser.py) each
reimplement domain locks + throttled GET + tenacity. They also keep separate
lock dicts, so HomepageClient and HeadlessBrowserClient do **not** actually
cooperate on the 1 req/sec/domain budget despite the comment claiming they do.
Extract a `ThrottledHTTPClient` in `sources/_http.py` with a shared registry.

### `techcrunch.py` reaches into private `NewsClient._fetch_text` [S]
[techcrunch.py:37](pipeline/src/nous/sources/techcrunch.py). Promote
`_fetch_text` to public or fold the TC adapter into `news.py`.

### Wellfound probe is mostly Cloudflare-blocked — demote it [S]
[estimate_employees.py](pipeline/src/nous/pipeline/estimate_employees.py) tries
Wellfound first, but it rarely returns data. Reorder the probe chain (or drop
Wellfound) so the common case doesn't burn a blocked request per company.

### Centralized prompt-input character limit [S]
Each LLM-using stage has its own truncation constant
([enrich_companies.py](pipeline/src/nous/pipeline/enrich_companies.py),
[funding_extraction.py](pipeline/src/nous/llm/prompts/funding_extraction.py)).
Centralize as `MAX_PROMPT_INPUT_CHARS` in `nous.llm.client`.

### Redundant `@pytest.mark.asyncio` decorators [S]
`asyncio_mode = "auto"` is set; explicit decorators across six test files
(`test_duckduckgo.py`, `test_robots.py`, `test_homepage.py`, `test_news.py`,
`test_vc_portfolios.py`, `test_employee_sources.py`) are no-ops. One sweep.

### Add `-rs` to the pytest invocation in CI [S]
[lint.yml:51](.github/workflows/lint.yml) runs `uv run pytest -v`; DB-gated
tests skip silently. `-rs` names the skips so a missing DATABASE_URL can't hide
test count.

---

## Frontend fixes (P2)

### Description-source attribution is misleading [S]
[c/[slug]/page.tsx](web/app/c/%5Bslug%5D/page.tsx) says "generated by … from
[hostname]" even when the description was derived from multiple pages. Soften
to "Generated on [date]" or track per-description sources.

### Missing Supabase env collapses into 404 [S]
[queries.ts](web/lib/queries.ts) returns `null`/empty indistinguishably for
"missing env" vs "no row", so a misconfigured deployment 404s every page.
Throw at module load (server-only) so misconfigs fail fast and loud.

### Total-raised tile may double-count overlapping rounds [S]
The detail page sums `amount_raised` across all rounds; if
`reconcile_funding_round` ever fails to merge two articles about the same round,
the tile double-counts. Document the assumption near the sum; longer-term add a
`round_correction_of` pointer for amended rounds.

### `formatUsd` rounding collapses distinct amounts [S]
$1.51M and $1.49M both render as "$1.5M" with no way to see exact figures.
Show exact dollars in a `title` tooltip.

---

## Product backlog — Wave 1: free wins

All buildable from data already in the DB; mostly frontend.

### SEO pack [M]
The site has no `sitemap.xml`, no `robots.txt`, no canonical URLs, no
Organization JSON-LD, no OG/Twitter cards. For a programmatic-content site this
is the cheapest distribution lever that exists. Includes dynamic OG images via
`@vercel/og` (free) showing name / industry / total raised. Subsumes the old
"No JSON-LD / canonical" backlog item.

### Investor pages — `/investor/[slug]` [M]
Portfolio, rounds led, co-investors, recent activity. `company_investors` and
`funding_round_investors` already hold everything; this is pure frontend + one
query module. "What is Sequoia buying lately" is the killer page for the VC
audience, and a major programmatic-SEO surface.

### "New this week" feed [S]
Homepage section + `/new` page listing companies discovered and rounds
extracted in the last 7 days (`created_at` queries). The pipeline runs weekly
but the site never says what's new — cheapest possible freshness signal.

### Tag pages — `/tag/[tag]` [S]
`companies.tags` already exists. Long-tail SEO, trivial build.

### Location pages — `/location/[state]` (and city) [S]
"Startups in Austin" from `hq_city`/`hq_state` we already extract.

### Per-page freshness line [S]
"Profile updated May 30" from `last_enriched_at`. Honesty beats implied
freshness, and it's one line in the header meta strip.

### "Report incorrect data" link [S]
Prefilled GitHub-issue URL on every company page. Crowdsourced QA, zero backend.

### Name-quality pass [S]
Prefer the company's own `og:site_name` / `<title>` casing (already in
`raw_pages`) over VC-portfolio casing. Folds in the old `name_quality`
source-priority idea: rank sources, overwrite only on higher quality.

### Logos via favicon fetch [S]
`companies.logo_url` exists and is mostly unused. Fetch
`/favicon.ico`/`apple-touch-icon` during scrape-homepages; render on cards and
detail header.

### Start history snapshots now [S]
New `company_snapshots` table (employee counts, job-posting counts, captured
weekly). Costs nothing today; Wave 4's momentum charts need the backfill.
Record first, render later.

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

### Market map — `/map/[industry]` [L]
The codebase's first client component: d3-force + hand-rolled canvas renderer
(~10–15KB gz), compact index-referenced JSON passed as props from a server
component (no API route, key stays server-side). Nodes sized by funding,
colored by theme, click → company page. Global view is a theme-level meta-graph
(~100 nodes), never the full raw graph. Fallback lib: `react-force-graph-2d`.

### `slug_aliases` table with 301 redirects [M]
Promoted from Future: dedup merges actively delete loser rows today, burning
inbound links and SEO equity. Keep old slug → 301 → new slug; middleware in
`web/` reads the table. Record aliases at merge time in
[dedup_companies.py](pipeline/src/nous/pipeline/dedup_companies.py).

---

## Product backlog — Wave 3: intelligence ("what's evolving")

### Embeddings infrastructure [M]
pgvector (free on Supabase; `CREATE EXTENSION vector` in a migration) +
`companies.embedding vector(384)`. Generate with fastembed
(`BAAI/bge-small-en-v1.5`, ONNX, CPU) inside GitHub Actions — $0, seconds per
run; optional uv dependency group so the main install stays light; cache the
model dir. ~8MB storage at 5k companies; exact scan is fine, no index needed.

### Semantic search [M]
"Startups doing AI for logistics" — embed the query, nearest-neighbor over
company embeddings, blend with the existing ilike search on the index page.

### Themes pipeline + pages [L]
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
