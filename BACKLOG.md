# Backlog

Open issues found during the M1–M4 audit (2026-05-27) and ongoing work. Add new
entries at the bottom of the appropriate section. Close items by deleting them
(or move to a `CHANGELOG.md` if you want a history).

**Severity:**
- **P1** — should fix before or during M5
- **P2** — operationally annoying but not corrupting; fix opportunistically
- **Future** — speculative or large; needs a separate spec discussion first

---

## Pipeline correctness (P1)

### scrape_homepages refetches dead URLs every weekly run
[scrape_homepages.py:282-349](pipeline/src/nous/pipeline/scrape_homepages.py) eligibility query selects companies with zero `raw_pages`. On fetch failure (robots block, 404, network error) nothing gets persisted, so the same company is selected next week. Wasteful at scale, violates the spirit of "1 req/sec per domain throttle". Fix: add `companies.last_scrape_attempt_at TIMESTAMPTZ` (migration), update on every attempt, gate eligibility with a back-off window (e.g. 30 days for robots-blocked, 7 days for network errors).

### Funding extraction never writes `valuation_source`
[funding_extraction.py](pipeline/src/nous/llm/prompts/funding_extraction.py) Pydantic model doesn't include the field; [upsert.py:402](pipeline/src/nous/db/upsert.py) docstring mentions it but no write path exists. The frontend now renders it ([FundingHistory.tsx:81-99](web/components/FundingHistory.tsx)) but the column will always be NULL. Fix: add `valuation_source` to `FundingExtraction`, prompt for "if a publication is named alongside the valuation, return its name + month"; wire into `reconcile_funding_round`.

### extraction_confidence is free-text in the DB
[models.py:193](pipeline/src/nous/db/models.py) is `String, nullable=True`. Application code uses `_CONFIDENCE_RANK.get(new, -1)` ([upsert.py:370](pipeline/src/nous/db/upsert.py)); a typo (`"medum"`) ranks as `-1` and silently downgrades the row. Pydantic catches typos at the LLM boundary but the DB is permissive. Fix: add `CheckConstraint("extraction_confidence IN ('low','medium','high') OR extraction_confidence IS NULL")` via a new migration.

---

## Pipeline cleanups (P2)

### TC-path `auto_create_company` ignores the configured similarity threshold
[ingest_news.py:172-181](pipeline/src/nous/pipeline/ingest_news.py) calls without `similarity_threshold=`, defaulting to 0.85. Today this matches `Settings.COMPANY_FUZZY_MATCH_THRESHOLD` but a future config tweak silently desyncs the VC and TC paths. Fix: plumb the setting through `run_ingest_news` like [refresh_vc_portfolios.py:99-106](pipeline/src/nous/pipeline/refresh_vc_portfolios.py) does.

### find_company_by_name over-matches with short normalized names
[upsert.py:301-309](pipeline/src/nous/db/upsert.py): trigram similarity is unstable for very short strings. "AI", "Vue", "X" can fuzzy-match unrelated companies at 0.85. Fix: add a minimum-length guard (`if len(norm) < 6: return None`) inside the trigram branch.

### Slug random disambiguator is non-deterministic for non-CIK rows
[slugify.py:108](pipeline/src/nous/util/slugify.py): `os.urandom(3).hex()` for the auto-create path. Two genuinely-different "Acme" rows produce different suffixes on different runs — bad for reproducible test fixtures. Fix: use a content hash (e.g. `sha256(name + (website or ''))[:6]`) so the same input always yields the same disambiguator.

### Competitor self-reference is not blocked at the DB
[models.py:259-272](pipeline/src/nous/db/models.py): nothing prevents `company_id == competitor_company_id`. If the LLM ever names a company as its own competitor and the exact-match resolver catches it, we'd render "Acme is a competitor of Acme". Fix: `CheckConstraint("competitor_company_id IS NULL OR competitor_company_id != company_id")` via a new migration.

### competitors.rank not enforced contiguous 1..N
[analyze_competitors.py:210-220](pipeline/src/nous/pipeline/analyze_competitors.py) trusts the LLM's `rank` field as ordinal. Sparse ranks (1, 2, 5) render fine but the UX is "Top 3, then #5". Fix: re-rank `resolved` to 1..N before insert in `run_analyze_competitors`.

### news_articles.url indexed twice
[0003_m3_schema.py:86-89](pipeline/alembic/versions/0003_m3_schema.py): both `UniqueConstraint("url")` and a redundant `create_index(..., unique=True)`. Two unique indexes for the same column waste write cost. Fix: drop the redundant `ix_news_articles_url` index.

### Throttle/get helper triplicated across source clients
[homepage.py:128-166](pipeline/src/nous/sources/homepage.py), [news.py:218-250](pipeline/src/nous/sources/news.py), [headless_browser.py:107-159](pipeline/src/nous/sources/headless_browser.py) all reimplement `_get_domain_lock` + `_throttled_get` + tenacity decoration. ~60 lines of duplication. The HomepageClient and HeadlessBrowserClient comment says they "cooperate when targeting the same host" but they keep separate dicts — they don't actually cooperate. Fix: extract a `ThrottledHTTPClient` base or mixin in `sources/_http.py`.

### techcrunch.py reaches into private NewsClient._fetch_text
[techcrunch.py:36](pipeline/src/nous/sources/techcrunch.py) uses `client._fetch_text` (private). Fix: either promote `_fetch_text` to public, or inline the TC adapter into `news.py` as `NewsClient.techcrunch_venture_feed`.

### Redundant `@pytest.mark.asyncio` decorators
`pyproject.toml` sets `asyncio_mode = "auto"`. Explicit `@pytest.mark.asyncio` decorators across `test_duckduckgo.py`, `test_robots.py`, `test_llm_client_deepseek.py`, `test_homepage.py`, `test_news.py`, `test_vc_portfolios.py` are no-ops. Fix: one-time sed sweep to remove.

### Add `-rs` to pytest invocation in CI
DB-gated tests are skipped silently. Adding `-rs` shows the skip list by name in CI logs so a missing DATABASE_URL doesn't hide test count. Fix: update `.github/workflows/lint.yml` step.

---

## Frontend (M5+ scope unless flagged)

### `/about` page is missing (header link 404s)
[layout.tsx:48-53](web/app/layout.tsx) links to `/about` but the route doesn't exist. Layout comment notes M5 per spec §7.1. **P2** — either remove the link until M5 or stub a methodology/sources page now.

### List page 500-row hardcoded limit, no pagination or search
[page.tsx:12](web/app/page.tsx) calls `listCompanies({ limit: 500 })`. Through M4 the DB likely has <500 rows so this is fine. As the DB grows, companies starting with N–Z silently disappear. M5 work, but at minimum add a "Showing N companies" hint above the grid before M5 ships.

### extraction_confidence queried but never gates UI
[FundingHistory.tsx](web/components/FundingHistory.tsx) reads the column but doesn't act on it. Low-confidence LLM extractions render as confident facts. Fix: render a muted `(low confidence)` pill or hide low-confidence rows behind a toggle.

### employee_count fields queried but never rendered
[types.ts:21-23](web/lib/types.ts): `employee_count_min/max/source` are on `CompanyRow` but no component renders them. Stays null today (no estimate-employees stage exists). When the M5 stage ships, add a "X–Y employees" line to the header meta strip gated on `employee_count_min != null`.

### Description-source attribution is misleading
[page.tsx:178-195](web/app/c/[slug]/page.tsx) says "generated by ... from [hostname]" even when the description was derived from multiple sources. Fix: soften to "Generated on [date]" or track per-description source.

### `getCompanyBySlug` collapses missing-env into 404
[queries.ts](web/lib/queries.ts) returns `null` indistinguishably for "missing Supabase env" vs "no row". A misconfigured deployment 404s every page instead of 500-ing loudly. Fix: throw on missing env at module load (server-only) so deployment misconfigs fail fast.

### Total-raised tile may double-count overlapping rounds
[page.tsx:90-92](web/app/c/[slug]/page.tsx) sums `amount_raised` across all rounds. If `reconcile_funding_round` ever fails to merge two articles about the same round (e.g. different `round_type` casing), the tile double-counts. Fix: add a comment near the sum acknowledging the assumption; longer-term, store a `round_correction_of` pointer for amended/corrected rounds.

### `formatUsd` rounding can collapse distinct amounts
$1.51M and $1.49M both render as "$1.5M". Cosmetic, but the user has no way to see exact filing amounts. Consider showing exact dollars on hover or in a detail expand.

### No JSON-LD / canonical link for SEO + agent-readability
Detail page has no `<link rel="canonical">` and no `Organization` JSON-LD. Easy SEO + LLM-readability win once the catalog stabilizes.

---

## Future ideas (need a spec discussion first)

### Post-ingest periodic dedup pass with human review
Catch near-duplicate companies that slipped through normalization (e.g. "Acme Robotics" vs "Acme Robotic Co"). Surface candidate merge pairs in a small admin view; never auto-merge.

### `company_aliases` table for stylization variants
Track every name we've seen per company + source. Lets us recover from a bad name choice without losing audit trail. Also unlocks "you searched 'OpenAI Inc' → here's the OpenAI page" UX.

### `slug_aliases` table with 301 redirects
If a company's primary slug ever changes (rename, merger), keep old slug → 301 → new slug. Middleware in `web/` reads from this table.

### `name_quality` score or source-priority column
Today first-discovery wins for `name`, with one cross-source upgrade path (lowercase → proper-cased). A future improvement: explicitly rank sources (e.g. proper-cased VC > news parse) and only overwrite when a higher-quality source arrives.

### Centralized prompt-input character limit
Each LLM-using stage has its own truncation constant ([enrich_companies.py:99](pipeline/src/nous/pipeline/enrich_companies.py), [funding_extraction.py:120](pipeline/src/nous/llm/prompts/funding_extraction.py)). Centralize as `MAX_PROMPT_INPUT_CHARS` in `nous.llm.client`.

### Add Vitest + one smoke test for `web/`
Currently zero web tests. `npm run build` typechecks but doesn't catch render-time bugs. A single Playwright happy-path ("/c/[slug] renders without throwing") would be high-leverage.
