# Design — Provenance UI ("make the moat visible"), ROADMAP Later #1

Written 2026-07-14, brainstormed + owner-approved. The design for turning
nous's "every fact is sourced / we don't hallucinate" moat into a **visible**
product feature on the company page. Read this, then root `CLAUDE.md`, then the
inventory below before touching code.

## Framing (the load-bearing nuance)

The moat is **"every rendered fact is sourced,"** which is *not* the same as
**"this company has lots of data."** Completeness (how many fields are filled) ≠
trustworthiness (the filled fields are sourced). A company can be *thin* (few
fields) yet every field it has is sourced.

So the surface is a **trust-builder, never a data-gap advertiser**:
- It always affirms **sourcing** (every figure links to a recorded source — the
  moat, always true) and **freshness** ("last verified N days ago").
- The **completeness badge is threshold-gated and positive-only**: it shows
  *"Richly documented"* / *"Well documented"* for high scores and shows
  **nothing** below the threshold — never a negative "thin/husk" badge that
  advertises gaps. (Same shape as `MomentumBadge`, which only lights for high
  momentum.)

## Data inventory (what already exists — mostly no migration)

**Company-level provenance columns (`pipeline/src/nous/db/models.py`):**
`website_source` + `website_source_url` (Wikidata / news_outbound / vc_portfolio),
`discovered_via`, `status_source_url`, `total_raised_source_url` +
`total_raised_as_of`, `last_enriched_at`, `website_resolved_at`,
`website_fallback_checked_at`, `news_checked_at`, `website_funding_checked_at`,
`employee_count_source` + `employee_count_checked_at`,
`consecutive_scrape_failures`, plus the `*_prompt_version` stamps.

**Per-round confidence + sources (`funding_rounds`):** `extraction_confidence`
(`low`|`medium`|`high`|null — only `low` is rendered today, as a pill in
`web/components/EventTimeline.tsx`), `primary_news_url`, `valuation_source`.

**Other entity provenance:** `people.source_url`, `competitors.source`/`source_url`,
`career_moves.source_url`, `company_relationships.source`/`evidence`,
`company_investors.source`.

**Completeness primitive (#175):** `pipeline/src/nous/util/completeness.py`
`completeness_score()` → 0.0–1.0 weighted over 9 fields (website 0.20,
description 0.20, funding 0.15, …), buckets husk(0–0.25) / thin(0.25–0.5) /
partial(0.5–0.75) / rich(0.75–1.0). **CI-only today** (emitted by
`pipeline/src/nous/pipeline/data_quality.py`); never reaches the web. This is the
**single source of truth** — the web must NOT re-implement it in TS.

**Web today:** `web/components/Sources.tsx` de-dupes + renders citations at the
page bottom; `StatusBadge`/`MomentumBadge` are the muted-pill vocabulary;
`web/app/c/[slug]/page.tsx` collects source_urls; `getCompanyBySlug`
(`web/lib/queries.ts`) returns the company row.

## Decomposition — 3 PRs (test often, merge often when green)

### PR 1 — pipeline: stored completeness score (husk pattern)
- **Migration 0042** (hand-written, chain off head **0041**): add
  `companies.completeness_score` (Float, nullable) + `completeness_computed_at`
  (timestamptz, nullable). No index (not a WHERE key; read per-company for
  display). Container-test the up/down round-trip on `pgvector/pgvector:pg15`.
- **New stage `compute-completeness`** (`pipeline/src/nous/pipeline/`): for every
  shown company, compute `util.completeness.completeness_score(...)` and write it
  + the stamp. Deterministic, $0, idempotent, `--limit` bounded; wire into
  `discovery.yml` after Snapshot/momentum (same cadence as `compute-momentum`).
  Mirror `compute_momentum.py`'s structure (summary, `record_pipeline_run`).
- Model field on `Company` + a DB-gated test. `ruff`+`mypy`+`pytest`.

### PR 2 — web: the "Data & provenance" panel on `/c/[slug]`
- `getCompanyBySlug` (or a small sibling read) also selects `completeness_score`,
  `last_enriched_at`, and the `*_checked_at` timestamps.
  **Migration-order-free:** an error/absent column → the badge/panel degrades to
  hidden (same pattern as momentum/map).
- New `ProvenancePanel` server component: a "Data & provenance" section with
  - the **positive completeness badge** (gated): `≥0.75 → "Richly documented"`,
    `0.5–0.75 → "Well documented"`, `<0.5 → no badge`;
  - **"Last verified N days ago"** — `MAX(last_enriched_at, website_resolved_at,
    website_fallback_checked_at, news_checked_at, website_funding_checked_at,
    employee_count_checked_at)`, computed read-time; `title` = the exact date;
  - a plain-language "Every figure here links to a recorded source" line tying to
    the existing `Sources` section (anchor link).
- Omit-when-empty; match badge/section styling. Component + query tests.

### PR 3 — web: granular per-fact sourcing
- **Inline source affordances:** a subtle muted superscript (↗ / "source") next
  to each already-sourced figure linking to its `source_url` (total-raised →
  `total_raised_source_url`; status → `status_source_url`; each funding row →
  `primary_news_url`; website → `website_source_url`). **Biggest visual risk —
  keep it subtle (existing muted vocabulary) and tune against a real build.**
- **Source-type labels** in `Sources.tsx`: "News / Website / Wikidata / VC
  portfolio" inferred from `website_source` + URL host.
- **Confidence transparency** in `EventTimeline`: surface `extraction_confidence`
  as a `title` tooltip on ALL rounds ("Extracted with high/medium/low
  confidence"); keep the **visible pill only for `low`** (the warning) — a
  "high confidence" pill on every row would be noise.

## Key decisions (locked)
- Completeness label map is **positive-only** (no negative badge below 0.5).
- `util.completeness` stays the **sole scorer**; the web never re-derives it
  (hence PR 1's stored column).
- "Last verified" is **read-time** (no dedicated column).
- Confidence shown as **tooltip-on-all + pill-only-for-low**, not a tri-state
  pill wall.

## Non-goals / follow-ups
- No per-round numeric confidence score (the enum stays).
- No `extraction_confidence` on people/competitors/career_moves (would need
  pipeline changes) — a later enhancement.
- A public data-quality dashboard (the web-facing version of #175's aggregate)
  is a separate Later item, not this feature.

## Verification
Per `CLAUDE.md`: `ruff`+`mypy`+`pytest` in `pipeline/` (container for the
migration), `lint`+`test`+`build` in `web/`. Adversarial `code-reviewer` per
branch. Verify the FULL `statusCheckRollup` green before each squash-merge.
