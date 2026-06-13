# Startup relationship graph — design

**Date:** 2026-06-12
**Status:** Approved (autonomous — see note)
**Author:** Claude (CTO partner), at the user's standing request

> **Approval note.** The user asked for this feature and explicitly delegated
> design + implementation while they were asleep ("I won't be able to approve
> the plan, but use superpowers brainstorming and planning … then implement").
> The interactive approval gate of the brainstorming skill is therefore replaced
> by the user's standing pre-authorization. Every non-obvious decision below
> records its reasoning and the assumption it rests on, so it can be reviewed
> after the fact and reversed cheaply.

## 1. Goal

Make nous feel like a **wiki of US software startups**, where each company links
out to the other companies it's meaningfully related to — competitors, peers in
the same space, and companies backed by the same investors. The user framed it
as: *"creating mappings via similar industries, potential competitors, supply
chain dependency, and/or other factors … like a wiki where similar topics/themes
are related and linked together."*

This is the first user-visible payoff of **`BACKLOG.md` Wave 2 — the
relationship graph (differentiator)**, which this design follows and refines.

## 2. What already exists (and why we build on it)

- **`competitors` table** — a directed company→company edge (`company_id` →
  `competitor_company_id`, a nullable self-FK), ranked, with `reasoning` +
  `source` (`techcrunch` | `llm_inferred`) + `source_url`. But
  `analyze-competitors` only resolves `competitor_company_id` by **exact**
  `normalized_name`, so many edges are "dangling" (name-only, FK NULL).
- **`company_investors`** — company→investor links from VC portfolio scraping.
  Powers "also backed by".
- **`companies.industry_group` / `primary_category` / `tags[]`** — free-text
  similarity signals produced by `enrich-companies`.
- **`pg_trgm`** is installed (fuzzy name matching). No embeddings / `pgvector`.
- **Web `/c/[slug]`** already renders a `Competitors` section; internal links are
  `/c/${slug}`. No "related/similar companies" surface exists yet.

Every fact rendered on a company page must carry a source (project rule). The
relationship graph honors this: every edge records where it came from.

## 3. Scope

### v1 — build now (zero-LLM, sourced entirely from existing data)

A relationship is only ever asserted from data we already hold, so there is **no
fabrication risk** and every edge is attributable.

1. **`company_relationships`** — a typed, directed edge table (the unified graph).
2. **`link-competitors`** stage — fuzzy-resolve dangling `competitors` edges
   (pg_trgm), turning name-only competitors into internal links. Zero LLM.
3. **`derive-relationships`** stage — set-based, replace-style, zero LLM. Projects
   resolved competitor edges into `company_relationships` and computes
   **similar** edges (same `industry_group` + `tags` overlap, top-K per company).
4. **Web: a "Related companies" section** on `/c/[slug]` — "Similar companies"
   (from `company_relationships`) + "Also backed by …" (a capped, read-time
   shared-investor query). Competitors keep their existing section.
5. **CI** — run `link-competitors` → `derive-relationships` weekly in
   `discovery.yml`, right after `analyze-competitors`.

### Deferred — explicitly out of scope for v1 (noted so they aren't lost)

- **LLM supply-chain / partner extraction.** `BACKLOG.md` gates this behind a
  ~$0.50 dry-run over ~20 companies to measure yield + hallucination rate,
  because funding news rarely names vendors and customer logos are images. That
  review needs a human; shipping un-reviewed LLM supply-chain links would risk
  fabricated facts. The `relationship_type` enum **includes** `supplier` /
  `customer` / `partner` so the schema is ready, but v1 does not populate them.
- **Embeddings / semantic "similar".** `BACKLOG.md` Wave 3 (pgvector + fastembed).
  v1's `industry_group` + `tags` overlap is a good-enough first cut; the `source`
  column leaves room for an `embedding` source later.
- **Market map `/map/[industry]`** — the codebase's first client component; large,
  separate effort.
- **"Alternatives to X" / "X vs Y" SEO pages** — derivable from the same edges;
  a stretch goal after the core graph lands.

## 4. Data model

New table `company_relationships` (follows every project convention: UUID PK +
`created_at`/`updated_at` from `Base`, FK indexes, a CHECK enum, idempotency
constraint). Directed storage — one row per `(company_id → related_company_id,
type)` — because the dominant read is "show everything related to company X",
which becomes a trivial `WHERE company_id = X`.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | from `Base` |
| `company_id` | UUID FK→companies, **CASCADE**, NOT NULL, indexed | the subject |
| `related_company_id` | UUID FK→companies, **CASCADE**, NOT NULL, indexed | the target; both endpoints are in-DB (resolved edges only) |
| `relationship_type` | String NOT NULL, indexed | CHECK ∈ `('competitor','similar','supplier','customer','partner')` |
| `score` | `Numeric(6,3)` NOT NULL | ordering / strength (similarity score, or `1/rank` for competitors) |
| `source` | String NOT NULL | provenance: `'competitors'` \| `'industry_tags'` \| (`'llm_inferred'` reserved) |
| `evidence` | String, nullable | short human-readable reason ("Both in developer-tools; 4 shared tags") |

Constraints / indexes:
- **UNIQUE `(company_id, related_company_id, relationship_type)`** —
  `uq_company_relationships_pair_type`; the idempotency key.
- **CHECK `related_company_id <> company_id`** —
  `ck_company_relationships_no_self` (mirrors `ck_competitors_no_self_reference`).
- Index `company_id`, `related_company_id`, `relationship_type` (FK + WHERE rule).
- Hand-written Alembic migration (the 0015–0021 convention: autogenerate emits
  spurious DROPs for the trigram/partial indexes).

**`merge_companies` integration (non-negotiable).** Company dedup deletes loser
rows; any company-referencing table must be repointed/deduped in
`db/upsert.py::merge_companies` or merges trip the self-edge CHECK / unique
constraint. `company_relationships` is handled exactly like the `competitors`
block: delete the loser's would-be self-edges first, repoint both `company_id`
and `related_company_id` to the survivor, then drop duplicates that collapse onto
an existing `(company_id, related_company_id, type)` triple.

**Why `shared_investor` is NOT a stored type.** `BACKLOG.md` is explicit: a
mega-investor (YC backs thousands) makes shared-investor a materialized O(N²)
blow-up. So "also backed by" is computed **at read time**, capped, with
high-degree investors excluded (>30 holdings) — never written to the table.

## 5. Pipeline stages (both zero-LLM)

### `link-competitors`
For each `competitors` row with `competitor_company_id IS NULL`, fuzzy-match
`competitor_name` against `companies.normalized_name` via pg_trgm
`func.similarity` (the pattern already in `dedup_companies.py`), **best-match
only with a tie guard** (skip when the top two candidates are within a small
margin — avoid wrong links), threshold ≥ 0.45. Only UPDATE NULL FKs; never
overwrite a resolved one; skip a match that equals `company_id` (the self-edge
CHECK). Idempotent (re-runs touch only still-NULL rows). `--limit` + `--dry-run`.
This directly enriches the existing Competitors UI **and** the derived graph.

### `derive-relationships`
Set-based, **replace-style**, zero LLM. In one run:
1. **competitor edges** — project every `competitors` row with a non-NULL
   `competitor_company_id` into `company_relationships`
   (`type='competitor'`, `source='competitors'`, `score = 1/rank`,
   `evidence = reasoning`). Dedup onto the unique triple.
2. **similar edges** — group companies by `industry_group`; for each company,
   score same-group peers by `2·|shared tags| + (same primary_category)`,
   require score ≥ 1 (at least one shared tag or shared category — never link a
   whole coarse industry indiscriminately), keep **top-K = 8**. Store as
   directed edges (`type='similar'`, `source='industry_tags'`,
   `score`, `evidence`). Bounded at K·N rows. Computed in Python (load
   companies + tags once, score within group) — a few million cheap set ops, fine
   for a weekly set-based stage.

Replace-style write per `source` (delete this source's edges, re-insert) inside a
transaction, so the stage is idempotent and self-healing. Runs weekly in
`discovery.yml` after `analyze-competitors` → `link-competitors`.

## 6. Web

- `web/lib/queries.ts`:
  - `getRelatedCompanies(companyId)` — reads `company_relationships` for
    `type='similar'` (and competitor edges are already shown elsewhere), joined to
    `companies` for slug/name/description, ordered by `score desc`, capped (~12).
  - `getAlsoBackedBy(companyId)` — read-time two-hop over `company_investors`:
    other companies sharing ≥1 investor, **excluding investors with > 30
    holdings**, ranked by shared-investor count, capped (~8). Mirrors the existing
    nested-join idiom; returns the shared investor names for the evidence line.
- `web/components/RelatedCompanies.tsx` — server component. Renders, when
  non-empty, a **"Similar companies"** group and an **"Also backed by"** group,
  each a compact list of internal `/c/${slug}` links with a one-line evidence /
  shared-investor caption (source attribution). Renders `null` when empty
  (graceful degradation — sparse data is the norm).
- `web/app/c/[slug]/page.tsx` — fetch both in the existing `Promise.all`, render
  `<RelatedCompanies>` directly after `<Competitors>` and before `<News>`.
- No new npm deps (codebase constraint). Reuse `CompanyCard` / pill patterns.

## 7. Testing

- DB: model + migration round-trips; `merge_companies` repoints/dedupes
  relationship edges and never trips the CHECK/unique (mirror the competitors
  merge tests).
- `link-competitors`: resolves a dangling edge to the right company; tie guard
  skips ambiguous matches; never overwrites a resolved FK; idempotent re-run.
- `derive-relationships`: projects competitor edges; computes similar edges with
  correct top-K + score; replace-style idempotency; respects the self/unique
  constraints.
- Web: query helpers narrow joins through interfaces (no `any`), degrade to empty
  on missing env, and exclude high-degree investors. `npm run build` typechecks.

Gates: `ruff check`, `mypy src`, `pytest` (pipeline) + `npm run build` (web).

## 8. Build order (critical path → parallel)

1. **DB foundation** (critical path): migration + model + `merge_companies`
   wiring + tests.
2. **Pipeline stages** (parallel after 1): `link-competitors` + `derive-relationships`
   + CLI + tests.
3. **Web** (parallel after 1): query helpers + `RelatedCompanies` + page wiring.
4. **CI wiring**: add both stages to `discovery.yml`.
5. **Integration**: full gates green; PR(s); merge. The migration applies on the
   next `pipeline.yml` run; the graph populates on the next `discovery.yml` run
   (or a manual dispatch).

## 9. Risks & mitigations

- **Fabrication / trust** — mitigated by zero-LLM v1; every edge is sourced from
  existing structured data.
- **O(N²) blow-up** — `similar` is capped top-K per company; `shared_investor` is
  read-time + high-degree-investor exclusion (never materialized).
- **Dedup integrity** — `merge_companies` wiring + tests.
- **Empty until populated** — the section degrades to nothing; populates on the
  next weekly discovery run or a manual `workflow_dispatch`.
- **Migration safety** — additive (new table only), hand-written, applies via the
  standard Actions `alembic upgrade head`.
