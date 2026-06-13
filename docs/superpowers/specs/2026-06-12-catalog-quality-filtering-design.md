# Catalog Quality Filtering — Design

Date: 2026-06-12
Status: approved by product owner (brainstorm session)

## Problem

Production catalog (4,218 companies as of 2026-06-12) contains entries that are not
US software startups, or that render as broken/empty pages. Investigation of the
reported examples found **four distinct failure modes**:

1. **Parse-artifact names (96 rows).** The Lightspeed adapter
   (`pipeline/src/nous/sources/vc_portfolios/lightspeed.py`) reads each portfolio
   card heading with `heading.text(strip=True)`, which concatenates a fund-badge
   child element into the name — producing e.g. `1047 gamesLSVP and LSIP Investment`.
   The `LSIP` badge means *Lightspeed India Partners*: the adapter is ingesting
   Lightspeed's India portfolio (OYO, Byju's, Grab, Darwinbox, …), which violates
   the US-only scope (`nous-technical-spec.md` §1.2 non-goals).
2. **Wrong or parked websites (~41 rows with parked-page descriptions).** The
   homepage resolver (`resolve_homepage()` in `pipeline/src/nous/sources/homepage.py`)
   guesses TLDs from the slug and accepts any page whose visible text mentions the
   company name. Parked "domain for sale" pages always mention the domain name, so
   they pass — e.g. real companies 9GAG (real site 9gag.com), Substack, Cameo, and
   Oklo got parked `*.ai` domains attached. Enrichment then honestly writes "this
   domain is listed for sale" prose, which the site renders.
3. **Real companies that are not startups.** VC portfolio pages list decades-old
   investments ([24]7.ai founded 2000 via Sequoia; 9GAG founded 2008). No maturity
   or eligibility check exists anywhere in the pipeline.
4. **Husk rows.** 2,590/4,218 companies have no description; 1,475 have no website.
   Husks render as name-only pages that look fake even when the company is real
   (`/dev/agents` is a real ~$56M-seed startup we simply have no data for yet).

A naive prose-pattern filter is not viable: a scan for "for sale" in descriptions
false-matched SellRaze, a real company whose description mentions listing items
for sale. Signals must be structured.

## Decisions (product owner, 2026-06-12)

- **Catalog bar:** a company is listed publicly iff it is not excluded AND
  (it has a real description OR ≥1 recorded funding round). Hidden rows stay in
  the DB and surface automatically once the pipeline learns something.
- **Startup test:** LLM judgment during the existing enrichment call (not a bare
  founding-year cutoff). Unknown → keep. Confident "not a startup" → exclude.
- **Mechanism:** soft exclusion via an `exclusion_reason` column, not render-time
  pattern filtering (fragile, wastes pipeline quota) and not hard deletion
  (weekly portfolio re-discovery would resurrect deleted rows). Hard delete is
  used only in the one-time repair for Lightspeed-India husk rows, which the
  fixed adapter will never re-emit.

## Design

### 1. Schema (one Alembic migration)

New columns on `companies`:

| Column | Type | Meaning |
|---|---|---|
| `exclusion_reason` | text, null | null = included. Values: `parse_artifact`, `non_us`, `not_a_startup`, `manual`. |
| `exclusion_detail` | text, null | Free-form audit note (e.g. the LLM's reason). |
| `excluded_at` | timestamptz, null | When the exclusion was set. |
| `eligibility_checked_at` | timestamptz, null | When the startup-judgment last ran; lets the backfill find enriched-but-unjudged rows and prevents re-judging forever. |
| `rejected_urls` | jsonb (list of strings), default `[]` | URLs confirmed wrong for this company; the resolver must never re-pick them. |
| `funding_round_count` | int, not null, default 0 | Denormalized count maintained by the funding stage, backfilled in the migration. Lets the catalog bar be an indexed WHERE instead of a join PostgREST paginates poorly. |

Indexes: partial index supporting the listing query (`WHERE exclusion_reason IS NULL`),
per the house rule of indexing every WHERE column.

### 2. Lightspeed adapter fix

Strip the fund-badge child node from the card `h5` before reading text (exact
selector confirmed against live lsvp.com markup during implementation; capture an
HTML fixture for regression tests). Skip entries whose badge is **LSIP-only** —
those are Lightspeed India portfolio companies, out of scope. Entries badged
"LSVP and LSIP" or unbadged are kept with clean names. The badge is not a perfect
HQ signal; the enrichment-time `hq_country` judgment (§5) is the backstop.

### 3. One-time repair stage (idempotent CLI command)

New pipeline stage `repair-catalog` registered in `cli.py`:

- For each row whose name ends with the badge suffix (96 today):
  - Suffix `LSIP Investment` only → **delete** the row if it is a husk (no funding
    rounds or news links); if it has accrued links, soft-exclude as `non_us` instead.
  - Suffix `LSVP and LSIP Investment` → strip suffix, regenerate
    `slug`/`normalized_name`; on collision with an existing clean-named company,
    merge into it via the existing dedup-merge machinery.
- For each row whose enrichment flagged a non-operational website (the ~41 parked
  rows; identified in the one-time pass by the known prose patterns, with the list
  reviewed by eye before applying): clear `website` and both descriptions, append
  the bad URL to `rejected_urls`, leaving the row for future re-resolution.

Running the stage twice is a no-op: no suffixed names remain after the first
pass, and the parked-row reset selects on the prose patterns in
`description_short` — which the first pass clears — so a second run matches
nothing.

### 4. Homepage resolver: reject parked pages

In `resolve_homepage()`, before the name-mention acceptance check:

- Reject candidates whose content matches parked/for-sale signatures: registrar
  and marketplace templates (Spaceship, GoDaddy, Sedo, Dan.com, Atom, Saw.com,
  Afternic, Namecheap parking, Reg AI, …) and phrases ("this domain is for sale",
  "buy this domain", "domain may be for sale", "is parked"). The indicator list
  lives in code, is conservative, and is unit-tested against captured fixtures.
- Skip any candidate URL present in the company's `rejected_urls`.

### 5. Enrichment: structured signals instead of prose

`CompanyDescription` (`pipeline/src/nous/llm/prompts/company_description.py`)
gains fields, all riding the existing enrichment LLM call (DeepSeek; no new LLM cost):

- `website_state`: required enum — `ok | parked_or_for_sale | under_construction |
  unrelated_site | insufficient_info`.
- `is_startup`: bool | null. Guidance in prompt: an independent, private company,
  founded within roughly the last 15 years, not a subsidiary, not publicly traded.
  Null when the text doesn't support a confident call.
- `not_startup_reason`: str | null.
- `founded_year`: int | null (only if stated; never guessed).
- `hq_country`: str | null (only if stated; never guessed).

Pipeline reactions in `enrich_companies.py`:

- `website_state != ok` → do **not** write descriptions; append the URL to
  `rejected_urls`; clear `website`. This is *not* an exclusion — 9GAG/Substack-type
  cases are real companies with a wrong URL attached. The row stays hidden by the
  catalog bar until a real site resolves and enriches.
- `is_startup == false` → `exclusion_reason = 'not_a_startup'`,
  `exclusion_detail = not_startup_reason`. Descriptions are still stored for audit.
  When `website_state != ok`, `is_startup` is ignored — a parked or unrelated page
  supports no judgment.
- `hq_country` non-null and not the US → `exclusion_reason = 'non_us'`. The prompt's
  null-over-guess rule is what makes a non-null value a confident one.
- Every enrichment sets `eligibility_checked_at`.

**Backfill:** a `judge-eligibility` stage runs only the eligibility judgment over
already-enriched companies (`description_short IS NOT NULL AND
eligibility_checked_at IS NULL`, ~1,600 rows) from their stored `raw_pages` text,
bounded by `--limit 200` per daily run on DeepSeek (~1,600 one-time calls ≈ $1–3;
resumable because progress is tracked by `eligibility_checked_at`).

### 6. Pipeline stages skip excluded rows

`scrape_homepages`, `resolve_homepages`, `enrich_companies`, and per-company news
polling add `exclusion_reason IS NULL` to their selection queries — excluded rows
stop consuming scrape budget and LLM quota. `auto_create_company` matching an
excluded row records attribution as usual but never clears the exclusion
(re-appearing on a portfolio page is not new evidence). A small
`exclude-company <slug> --reason manual` CLI command provides a hand lever for
one-offs without raw SQL.

### 7. Web: one central catalog filter

A single shared filter in `web/lib/queries.ts` applied to the browse list, search,
industry dropdown, and front-page counts:

```
exclusion_reason IS NULL AND (description_short IS NOT NULL OR funding_round_count > 0)
```

Spotlight keeps its existing stricter rules plus the exclusion check. Detail
pages: excluded companies return 404; hidden-but-legit husks (like `/dev/agents`)
still render at their direct URL so links stay stable — they are simply unlisted
until they earn content.

### 8. Testing

- Lightspeed adapter: fixture HTML with badge variants → clean names; LSIP-only
  entries skipped.
- Parked detection: captured parked-page fixtures rejected; a real homepage
  fixture accepted.
- Repair stage: seeded-DB cases (suffix rename, collision merge, husk delete,
  linked-row exclude) plus run-twice idempotency.
- Enrichment reactions: mocked LLM returning each `website_state` /
  `is_startup` variant.
- Web: `npm run build` plus manual verification of list/counts consistency.

### 9. Rollout

1. Migration (applies via the Actions pipeline `alembic upgrade head`).
2. Pipeline fixes (adapter, resolver, enrichment, stage skips).
3. Run `repair-catalog` once via workflow dispatch; verify counts.
4. Web filter.
5. `judge-eligibility` backfill across a few daily runs.

Expected visible effect: 96 mangled names fixed or removed, ~41 parked-domain
pages reset, husk rows unlisted until enriched, decades-old non-startups excluded
as the judgment lands.

## Non-goals

- No change to `status`-based visibility (acquired/IPO/shut-down companies remain
  visible with status shown) — separate conversation if wanted.
- No manual review UI; the CLI lever suffices.
- No homepage-resolution re-architecture beyond parked rejection + `rejected_urls`.
- No IP/geo lookups for US-ness; LSIP-skip plus enrichment `hq_country` judgment only.

## Open implementation details (plan-level, not product)

- Exact lsvp.com badge selector and fixture capture.
- Contents of the parked-indicator list.
- How the repair stage reuses `dedup_companies` merge mechanics.
- Backfill throttle numbers against current DeepSeek spend.
