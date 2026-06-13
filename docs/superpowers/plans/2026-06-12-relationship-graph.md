# Relationship Graph Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ship a zero-LLM startup relationship graph — a `company_relationships` edge table populated by `link-competitors` + `derive-relationships` stages, surfaced as a "Related companies" section on `/c/[slug]`.

**Architecture:** New typed/directed edge table; two set-based zero-LLM pipeline stages project competitor edges + compute industry/tag similarity; a read-time capped shared-investor query powers "also backed by". Follows `BACKLOG.md` Wave 2 and the design at `docs/superpowers/specs/2026-06-12-startup-relationship-graph-design.md`.

**Tech Stack:** SQLAlchemy 2.x async + Alembic, pg_trgm, Click; Next.js 16 server components + Supabase.

**Execution model:** Task 1 (DB foundation) is the critical path — done in-session first. Tasks 2+3 (pipeline stages, share `cli.py`) and Task 4 (web, separate dir) parallelize after Task 1. Task 5 (CI) + Task 6 (gates/PR) close it out.

---

## File structure

| File | Action | Responsibility |
|---|---|---|
| `pipeline/src/nous/db/models.py` | modify | add `CompanyRelationship` model |
| `pipeline/alembic/versions/0022_company_relationships.py` | create | hand-written migration (new table + indexes + constraints) |
| `pipeline/src/nous/db/upsert.py` | modify | extend `merge_companies` to repoint/dedupe relationship edges; add `fuzzy_resolve_company_id` helper if not present |
| `pipeline/src/nous/pipeline/link_competitors.py` | create | fuzzy-resolve dangling `competitors.competitor_company_id` |
| `pipeline/src/nous/pipeline/derive_relationships.py` | create | replace-style projection of competitor + similar edges |
| `pipeline/src/nous/cli.py` | modify | register `link-competitors` + `derive-relationships` commands |
| `pipeline/tests/test_company_relationships_model.py` | create | model + merge integrity |
| `pipeline/tests/test_link_competitors.py` | create | stage tests |
| `pipeline/tests/test_derive_relationships.py` | create | stage tests |
| `.github/workflows/discovery.yml` | modify | run both stages weekly after `analyze-competitors` |
| `web/lib/types.ts` | modify | `CompanyRelationship` + related row types |
| `web/lib/queries.ts` | modify | `getRelatedCompanies` + `getAlsoBackedBy` |
| `web/components/RelatedCompanies.tsx` | create | render "Similar companies" + "Also backed by" |
| `web/app/c/[slug]/page.tsx` | modify | fetch + render `<RelatedCompanies>` after `<Competitors>` |

---

## Task 1: DB foundation (`CompanyRelationship` model + migration + merge wiring)

**Files:** modify `models.py`, `upsert.py`; create `0022_*` migration + `test_company_relationships_model.py`.

Model (mirror the `Competitor` model's conventions — `PG_UUID(as_uuid=True)`, `mapped_column`, `__table_args__` with `UniqueConstraint`/`CheckConstraint`, names like `ck_competitors_no_self_reference`):

```python
class CompanyRelationship(Base):
    __tablename__ = "company_relationships"

    company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    related_company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    relationship_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    score: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    evidence: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "related_company_id", "relationship_type",
                         name="uq_company_relationships_pair_type"),
        CheckConstraint("related_company_id <> company_id",
                        name="ck_company_relationships_no_self"),
        CheckConstraint(
            "relationship_type IN "
            "('competitor','similar','supplier','customer','partner')",
            name="ck_company_relationships_type"),
    )
```

Migration: hand-write `op.create_table(...)` + explicit `op.create_index` on `company_id`, `related_company_id`, `relationship_type`, plus the unique + check constraints. `down_revision = "0021"` (verify the current head with `uv run alembic heads`). `downgrade()` drops the table.

`merge_companies` (in `upsert.py`): after the existing `competitors` block, add a `company_relationships` block that (a) deletes loser rows that would become self-edges after repoint (`company_id == survivor` or `related_company_id == survivor`), (b) repoints `company_id` and `related_company_id` loser→survivor, (c) deletes duplicates colliding on the unique triple. Model it line-for-line on the existing competitor-merge handling.

- [ ] Write `test_company_relationships_model.py`: insert an edge; assert the unique triple rejects a dup; assert the self-edge CHECK rejects `related==company`; assert `merge_companies` repoints both endpoints and drops the resulting self-edge + duplicate.
- [ ] `uv run alembic upgrade head` against the local DB; confirm the table exists.
- [ ] `ruff check`, `mypy src`, run the new test file.
- [ ] Commit.

## Task 2: `link-competitors` stage

**Files:** create `pipeline/src/nous/pipeline/link_competitors.py` + `test_link_competitors.py`; modify `cli.py`.

Logic: select `competitors` rows with `competitor_company_id IS NULL`. For each, run a pg_trgm similarity query against `companies.normalized_name` (pattern from `dedup_companies.py::_generate_fuzzy_pairs` — `func.similarity`), order desc, take the top candidate **only if** `sim >= 0.45` AND (no second candidate OR top − second ≥ 0.08 tie-margin) AND candidate ≠ `company_id`. UPDATE the row's `competitor_company_id`. Per-row commit; `--limit` + `--dry-run`. Idempotent (only touches NULL FKs). Return a Pydantic summary (`rows_seen`, `linked`, `skipped_ambiguous`, `skipped_no_match`).

- [ ] Test: a dangling competitor whose `competitor_name` fuzzy-matches exactly one company gets linked; an ambiguous name (two close matches) is skipped; a resolved row is untouched; a self-match is skipped; re-run is a no-op.
- [ ] Register `link-competitors` in `cli.py` (mirror an existing command's structure, `--limit`/`--dry-run`, `emit_run_telemetry` not needed — zero LLM).
- [ ] `ruff`/`mypy`/test; commit.

## Task 3: `derive-relationships` stage

**Files:** create `pipeline/src/nous/pipeline/derive_relationships.py` + `test_derive_relationships.py`; modify `cli.py`.

Replace-style, zero LLM. Two sources, written in one run:
1. **competitor** — `INSERT INTO company_relationships SELECT company_id, competitor_company_id, 'competitor', 1.0/GREATEST(rank,1), 'competitors', reasoning FROM competitors WHERE competitor_company_id IS NOT NULL` with `ON CONFLICT (company_id, related_company_id, relationship_type) DO UPDATE`. Skip self-edges (the CHECK; filter `competitor_company_id <> company_id`).
2. **similar** — load `(id, industry_group, primary_category, tags)` for companies with non-null `industry_group`; group by `industry_group`; for each company score same-group peers `2*len(shared_tags) + (1 if same primary_category else 0)`, keep `score >= 1`, take **top 8** by score; upsert directed edges `(company_id, peer_id, 'similar', score, 'industry_tags', evidence)`.

Replace semantics: wrap each source in "delete `WHERE source = X` then re-insert" inside one transaction so the stage is idempotent/self-healing. `--limit` optional (cap companies scored for similar). Pydantic summary (`competitor_edges`, `similar_edges`).

- [ ] Test: seed 3 companies (2 same industry sharing tags, 1 different) + a resolved competitor row; run; assert competitor edge exists, similar edges link the two same-industry companies bidirectionally, the unrelated company has none; re-run is idempotent (counts stable, no dupes).
- [ ] Register `derive-relationships` in `cli.py`.
- [ ] `ruff`/`mypy`/test; commit.

## Task 4: Web — Related companies section (parallel, separate dir)

**Files:** modify `web/lib/types.ts`, `web/lib/queries.ts`, `web/app/c/[slug]/page.tsx`; create `web/components/RelatedCompanies.tsx`.

- `getRelatedCompanies(companyId)`: select `company_relationships` where `company_id = X AND relationship_type = 'similar'`, nested-join `related_company:companies!related_company_id(slug, name, description_short, status, industry_group)`, order `score desc`, limit 12. Narrow the join through a local interface (no `any`); degrade to `[]` on missing env (existing pattern).
- `getAlsoBackedBy(companyId)`: two-hop over `company_investors` — find this company's investors, exclude any investor with > 30 holdings (a count subquery/filter), find other companies sharing those investors, rank by shared count, limit 8. Return `{ slug, name, sharedInvestors: string[] }[]`.
- `RelatedCompanies.tsx` (server component): props = the two result sets. Render a "Similar companies" group (reuse `CompanyCard` or a compact linked list with the industry/shared-tag evidence) and an "Also backed by" group (linked list + the shared investor names as the source caption). `return null` when both empty. Follow `Competitors.tsx` for structure + attribution styling.
- `page.tsx`: add both fetches to the existing `Promise.all`; render `<RelatedCompanies …/>` directly after `<Competitors>` and before `<News>`.

- [ ] Build the queries + component + wire the page.
- [ ] `npm run build` (typechecks); verify the company page renders the section when data exists and renders nothing when empty.

## Task 5: CI wiring

**Files:** modify `.github/workflows/discovery.yml`.

Add two steps after `analyze-competitors`, before `snapshot-companies`: `link-competitors` (id `link_competitors`, continue-on-error, timeout 15) then `derive-relationships` (id `derive_relationships`, continue-on-error, timeout 15). Both carry an `id` so the deploy gate sees them.

- [ ] Add steps; validate YAML.

## Task 6: Integration + gates + PR

- [ ] From `pipeline/`: `ruff check .`, `mypy src`, full `pytest` — all green.
- [ ] From `web/`: `npm run build` — green.
- [ ] Open PR; merge after CI.

---

## Self-review

- **Spec coverage:** company_relationships table ✓ (T1); link-competitors ✓ (T2); derive-relationships competitor+similar ✓ (T3); related-companies module + also-backed-by ✓ (T4); CI ✓ (T5); merge_companies integrity ✓ (T1). Deferred items (LLM supply-chain, embeddings, market map, alternatives pages) intentionally absent.
- **Type consistency:** model fields (`company_id`, `related_company_id`, `relationship_type`, `score`, `source`, `evidence`) used identically across T1/T3/T4. Edge `source` values: `'competitors'`, `'industry_tags'`. `relationship_type` values match the CHECK enum.
- **No O(N²):** `similar` capped top-8/company; `shared_investor` read-time + >30-holding exclusion, never stored.
