// Server-side query helpers. This file must never be imported from a client
// component — it uses createSupabaseServerClient() which requires the service
// role key to be present in the server environment. The `server-only` import
// makes that a build-time guarantee, not a comment.

import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";

import { competitorLeaksMeta } from "@/lib/competitor-guards";
import { createSupabaseServerClient, SupabaseConfigError } from "@/lib/db";
import { computeTotalRaised, type QuarterTotal } from "@/lib/funding";
import { industryToSlug } from "@/lib/industry";
import { buildSpotlightPool, type Spotlight } from "@/lib/spotlight";
import type {
  AlsoBackedByCompany,
  AlternativeCompany,
  AlternativesData,
  CareerMove,
  CoInvestor,
  CompanyDetail,
  CompanyInvestorRow,
  CompanyListRow,
  CompanyRow,
  FactVerification,
  CompareCompany,
  CompetitorRow,
  CompetitorWithResolved,
  FundingRound,
  FundingRoundWithInvestors,
  HuskFallbackRow,
  InvestorDetail,
  InvestorListRow,
  InvestorPortfolioMomentum,
  InvestorRoundRow,
  InvestorSlugRow,
  MomentumCompany,
  PortfolioMomentumCompany,
  NamedAlternative,
  NewsArticleRow,
  PersonRow,
  RelatedCompany,
  SimilarCompany,
  ThemeDetail,
  ThemeListRow,
  ThemeMember,
  ThemeRound,
} from "@/lib/types";
// The badge threshold is the single source of truth for "heating up" — reused
// here so the portfolio-momentum count and the per-company badge agree.
import { MOMENTUM_BADGE_THRESHOLD } from "@/components/MomentumBadge";

/**
 * The server Supabase client, or null when Supabase is intentionally absent
 * (secret-free CI, local dev without .env.local) so the caller degrades to
 * empty results. A SupabaseConfigError — missing/partial env on Vercel, i.e. a
 * deployment mistake — is rethrown so the page errors loudly instead of
 * rendering an empty catalog that 404s every company (W-C.2).
 */
function supabaseOrNull(label: string): SupabaseClient | null {
  try {
    return createSupabaseServerClient();
  } catch (err) {
    if (err instanceof SupabaseConfigError) throw err;
    console.warn(`[${label}] Supabase not configured:`, (err as Error).message);
    return null;
  }
}

// Shape returned by the nested Supabase select on `funding_rounds`. We narrow
// rather than reach for `any` so the join structure is checked at the boundary.
interface NestedInvestor {
  name: string | null;
}

interface NestedFundingRoundInvestor {
  is_lead: boolean | null;
  investors: NestedInvestor | NestedInvestor[] | null;
}

type FundingRoundJoin = FundingRound & {
  funding_round_investors: NestedFundingRoundInvestor[] | null;
};

interface NestedResolvedCompany {
  slug: string | null;
  name: string | null;
  exclusion_reason?: string | null;
}

type CompetitorJoin = CompetitorRow & {
  competitor_company: NestedResolvedCompany | NestedResolvedCompany[] | null;
};

// Nested shape from the company_investors → investors select.
interface NestedInvestorFull {
  name: string | null;
  website: string | null;
}

type CompanyInvestorJoin = {
  is_lead: boolean | null;
  source: string | null;
  investors: NestedInvestorFull | NestedInvestorFull[] | null;
};

// Nested shape from company_relationships → related company. Same object-or-
// single-element-array ambiguity PostgREST gives every embed; narrowed here.
interface NestedRelatedCompany {
  slug: string | null;
  name: string | null;
  description_short: string | null;
  status: string | null;
  industry_group: string | null;
  // Excluded (junk/husk) companies 404 on /c/[slug]; carry the flag so related
  // links to them can be dropped rather than rendered as dead links.
  exclusion_reason?: string | null;
}

type CompanyRelationshipJoin = {
  score: number | null;
  evidence: string | null;
  related_company: NestedRelatedCompany | NestedRelatedCompany[] | null;
};

/**
 * Sort options exposed by the index page.
 * - name_asc / name_desc — alphabetical.
 * - recent — created_at desc (newest added to the catalog).
 * - funding_desc — biggest most-recent raise first (latest_round_amount desc,
 *   nulls last). (Task C1)
 * - recently_funded — most recently raised first (latest_round_date desc, nulls
 *   last). (Task C1)
 * - headcount_desc — largest headcount first (employee_count_max desc, nulls
 *   last). (Task C1)
 */
export type CompanyListSort =
  | "name_asc"
  | "name_desc"
  | "recent"
  | "funding_desc"
  | "recently_funded"
  | "headcount_desc";

/** Filters + paging accepted by {@link listCompanies}. */
export interface CompanyListOptions {
  search?: string;
  industry_group?: string;
  discovered_via?: string;
  /** Filter to companies whose `tags` array contains this exact value. */
  tag?: string;
  /** Filter to companies whose `hq_state` exactly matches this value. */
  state?: string;
  // ── Advanced VC filters (Task C2). All compose with .gte/.lte/.eq; every
  //    column below is indexed (year_incorporated/employee_count_* by prior
  //    migrations, total_raised_usd by 0021, latest_round_* by 0028). ──────────
  /** Minimum stated cumulative total raised, USD (`total_raised_usd >= n`). */
  min_raised?: number;
  /** Maximum stated cumulative total raised, USD (`total_raised_usd <= n`). */
  max_raised?: number;
  /** Founded in or after this year (`year_incorporated >= n`). */
  founded_after?: number;
  /** Founded in or before this year (`year_incorporated <= n`). */
  founded_before?: number;
  /** Minimum headcount (`employee_count_max >= n` — upper bound of the range). */
  emp_min?: number;
  /** Maximum headcount (`employee_count_min <= n` — lower bound of the range). */
  emp_max?: number;
  /** Exact latest funding stage, e.g. "Series A" (`latest_round_type = s`). */
  stage?: string;
  /** Only companies whose latest round is within the last N days. */
  funded_since_days?: number;
  sort?: CompanyListSort;
  limit?: number;
  offset?: number;
}

/**
 * Structural subset of the postgrest-js filter builder we chain in
 * {@link applyCompanyFilters}. PostgREST's PostgrestFilterBuilder generics
 * aren't publicly nameable (GenericSchema isn't exported), so typing the helper
 * against the concrete builder would force an `any` (forbidden by CLAUDE.md).
 * A generic `<Q extends CompanyFilterable>` that returns the SAME `Q` preserves
 * the builder's full type through the helper without any escape hatch — every
 * chained method returns the same instance type, so threading `Q` is sound.
 */
export interface CompanyFilterable {
  or(filters: string): this;
  eq(column: string, value: string): this;
  gte(column: string, value: string | number): this;
  lte(column: string, value: string | number): this;
  contains(column: string, value: readonly string[]): this;
}

/**
 * Apply every non-pagination/non-sort filter in {@link CompanyListOptions} to a
 * query builder. Shared by the main listing query, its count-fallback, and the
 * CSV-export keyset scan (Task C4) so all three apply the exact same filter
 * semantics and can never drift. The catalog bar + search `.or()` stay at the
 * call sites because they need the `count`/range context; everything
 * column-scoped lives here. Exported so the export route can reuse it.
 */
export function applyCompanyFilters<Q extends CompanyFilterable>(
  query: Q,
  opts: CompanyListOptions,
): Q {
  let q = query;
  if (opts.industry_group) q = q.eq("industry_group", opts.industry_group);
  if (opts.discovered_via) q = q.eq("discovered_via", opts.discovered_via);
  if (opts.tag) {
    // `contains` checks the text[] column includes the exact element. Never
    // ilike here — a substring match would conflate e.g. "ai" with
    // "ai-infrastructure".
    q = q.contains("tags", [opts.tag]);
  }
  if (opts.state) q = q.eq("hq_state", opts.state);
  if (opts.min_raised != null) q = q.gte("total_raised_usd", opts.min_raised);
  if (opts.max_raised != null) q = q.lte("total_raised_usd", opts.max_raised);
  if (opts.founded_after != null) {
    q = q.gte("year_incorporated", opts.founded_after);
  }
  if (opts.founded_before != null) {
    q = q.lte("year_incorporated", opts.founded_before);
  }
  // Headcount is a range [min, max]; "at least N employees" means the upper
  // bound reaches N, "at most N" means the lower bound is within N.
  if (opts.emp_min != null) q = q.gte("employee_count_max", opts.emp_min);
  if (opts.emp_max != null) q = q.lte("employee_count_min", opts.emp_max);
  if (opts.stage) q = q.eq("latest_round_type", opts.stage);
  if (opts.funded_since_days != null && opts.funded_since_days > 0) {
    const cutoff = new Date(Date.now() - opts.funded_since_days * 86400e3)
      .toISOString()
      .slice(0, 10); // latest_round_date is a DATE column (YYYY-MM-DD).
    q = q.gte("latest_round_date", cutoff);
  }
  return q;
}

/** Paged result: the current page of rows plus the total matching the filters. */
export interface CompanyListResult {
  rows: CompanyListRow[];
  total: number;
}

/**
 * Strip characters that have meaning in the PostgREST filter grammar so a
 * user-supplied search term can't break out of the `.or()` / `.ilike()`
 * expression (commas separate `.or()` clauses; `%`/`*` are wildcards).
 */
export function sanitizeIlikeTerm(term: string): string {
  return term.replace(/[,()%*\\]/g, " ").replace(/\s+/g, " ").trim();
}

/**
 * Catalog bar (spec 2026-06-12): a company is publicly listed iff it is not
 * excluded AND (it has a description OR ≥1 recorded funding round). Companies
 * failing the bar stay in the DB and reappear once the pipeline learns
 * something about them. Apply via:
 *   query.is("exclusion_reason", null).or(CATALOG_BAR_OR)
 * PostgREST ANDs the .or() group with every other filter (including a second
 * .or() such as the listCompanies search — repeated `or=` params AND-combine).
 *
 * Applied inline at each call site rather than via an applyCatalogBar(query)
 * helper on purpose: postgrest-js's PostgrestFilterBuilder generics aren't
 * publicly nameable (GenericSchema isn't exported), so a typed wrapper would
 * force an `any` — which CLAUDE.md forbids. A shared constant is the cleanest
 * fully-typed option.
 */
export const CATALOG_BAR_OR =
  "description_short.not.is.null,funding_round_count.gt.0";

/**
 * Return a filtered, sorted, paginated page of companies plus the total count
 * matching the filters (for pagination). Search matches `name` or
 * `description_short` (case-insensitive substring). Backed by the GIN trigram
 * index on `normalized_name` for the name side; `ilike` is adequate at current
 * scale. Funding-based sort would need a cross-table aggregate (Postgres view /
 * RPC) and is intentionally out of scope here.
 */
export async function listCompanies(
  opts: CompanyListOptions,
): Promise<CompanyListResult> {
  const limit = opts.limit ?? 30;
  const offset = opts.offset ?? 0;

  const supabase = supabaseOrNull("listCompanies");
  if (!supabase) return { rows: [], total: 0 };

  // `count: "exact"` makes PostgREST return the total matching the filters
  // (ignoring `.range()`), so we get rows + total in a single round trip.
  let query = supabase
    .from("companies")
    .select(
      "slug, name, hq_city, hq_state, industry_group, description_short, status, logo_url",
      { count: "exact" },
    )
    .is("exclusion_reason", null)
    .or(CATALOG_BAR_OR);

  const search = opts.search ? sanitizeIlikeTerm(opts.search) : "";
  if (search) {
    query = query.or(
      `name.ilike.%${search}%,description_short.ilike.%${search}%`,
    );
  }
  // Column-scoped filters (industry/source/tag/state + the Task C2 VC filters)
  // are applied by the shared helper so the count-fallback below can reuse the
  // exact same set and the two can never drift.
  query = applyCompanyFilters(query, opts);

  switch (opts.sort) {
    case "name_desc":
      query = query.order("name", { ascending: false });
      break;
    case "recent":
      query = query.order("created_at", { ascending: false });
      break;
    // Funding/recency/headcount sorts (Task C1) read the denormalized columns
    // from migration 0028 (latest_round_*) / employee_count_max. nullsFirst:
    // false keeps unfunded / headcount-unknown companies at the bottom. `name`
    // is a deterministic tiebreaker so paging is stable when many rows share a
    // null / equal sort value.
    case "funding_desc":
      query = query
        .order("latest_round_amount", { ascending: false, nullsFirst: false })
        .order("name", { ascending: true });
      break;
    case "recently_funded":
      query = query
        .order("latest_round_date", { ascending: false, nullsFirst: false })
        .order("name", { ascending: true });
      break;
    case "headcount_desc":
      query = query
        .order("employee_count_max", { ascending: false, nullsFirst: false })
        .order("name", { ascending: true });
      break;
    default:
      query = query.order("name", { ascending: true });
  }

  const { data: companies, error, count } = await query.range(
    offset,
    offset + limit - 1,
  );

  if (error) {
    // PostgREST returns PGRST103 "Requested range not satisfiable" when the
    // offset is beyond the last row. In that case the `count` header is still
    // returned (it reflects the filter set, not the range), but supabase-js
    // surfaces it as null when `error` is set. Fall back to a head-only count
    // so the page can clamp the requested page number rather than showing a
    // false "total=0" cold-start box.
    if (count != null) {
      return { rows: [], total: count };
    }

    // Fetch the count independently — we still have all the filters wired up.
    // Rebuild the count query from the same options (via the shared helper) so
    // it stays consistent with the main query.
    let countQuery = supabase
      .from("companies")
      .select(
        "slug",
        { count: "exact", head: true },
      )
      .is("exclusion_reason", null)
      .or(CATALOG_BAR_OR);

    const search2 = opts.search ? sanitizeIlikeTerm(opts.search) : "";
    if (search2) {
      countQuery = countQuery.or(
        `name.ilike.%${search2}%,description_short.ilike.%${search2}%`,
      );
    }
    countQuery = applyCompanyFilters(countQuery, opts);

    const { count: fallbackCount, error: countError } = await countQuery;
    if (countError) {
      console.error("[listCompanies] companies query failed:", error.message);
      console.error("[listCompanies] fallback count failed:", countError.message);
      return { rows: [], total: 0 };
    }

    // Return 0 rows but the real total — caller can clamp the page.
    return { rows: [], total: fallbackCount ?? 0 };
  }

  const rows = (companies ?? []).map((c) => ({
    slug: c.slug as string,
    name: c.name as string,
    hq_city: (c.hq_city as string | null) ?? null,
    hq_state: (c.hq_state as string | null) ?? null,
    industry_group: (c.industry_group as string | null) ?? null,
    description_short: (c.description_short as string | null) ?? null,
    status: c.status as string,
    logo_url: (c.logo_url as string | null) ?? null,
  }));

  return { rows, total: count ?? rows.length };
}

// ─── Semantic search (E-2) ────────────────────────────────────────────────────

/**
 * How many nearest neighbors the semantic RPC returns. Mirrors the /companies
 * page size (PAGE_SIZE) and the semantic_companies() SQL default (migration
 * 0035); passed explicitly so the SQL default and the UI cap can't drift
 * apart silently — same convention as SIMILAR_COMPANIES_LIMIT.
 */
const SEMANTIC_MATCH_COUNT = 30;

// Shape returned by the semantic_companies() Postgres function (migration
// 0035): the card-list projection + similarity. Narrowed rather than `any`,
// same as SimilarCompanyRpcRow.
interface SemanticCompanyRpcRow {
  slug: string | null;
  name: string | null;
  hq_city: string | null;
  hq_state: string | null;
  industry_group: string | null;
  description_short: string | null;
  status: string | null;
  logo_url: string | null;
  similarity: number | null;
}

/**
 * Companies nearest to a query embedding, by cosine similarity over the
 * pipeline-computed description embeddings — the semantic arm of /companies
 * search. PostgREST cannot ORDER BY a vector distance through filter params,
 * so the ranking lives in the `semantic_companies` SQL function (migration
 * 0035) and this helper calls it via `.rpc()`, passing the vector in
 * pgvector's `[x,y,...]` input format (a JSON array string).
 *
 * The function itself filters to shown (`exclusion_reason IS NULL`), embedded
 * companies that pass the catalog bar, so excluded companies never surface;
 * the slug/name guard here is the same defense-in-depth used by
 * getSimilarCompanies. Returns [] on missing env or error — semantic search
 * is an enhancement and must never break the page.
 */
export async function semanticCompanySearch(
  queryEmbedding: number[],
  matchCount: number = SEMANTIC_MATCH_COUNT,
): Promise<CompanyListRow[]> {
  const supabase = supabaseOrNull("semanticCompanySearch");
  if (!supabase) return [];

  const { data, error } = await supabase.rpc("semantic_companies", {
    query_embedding: `[${queryEmbedding.join(",")}]`,
    match_count: matchCount,
  });

  if (error) {
    console.error("[semanticCompanySearch] rpc failed:", error.message);
    return [];
  }

  return ((data ?? []) as SemanticCompanyRpcRow[]).flatMap((row) => {
    if (!row.slug || !row.name) return [];
    return [
      {
        slug: row.slug,
        name: row.name,
        hq_city: row.hq_city ?? null,
        hq_state: row.hq_state ?? null,
        industry_group: row.industry_group ?? null,
        description_short: row.description_short ?? null,
        status: row.status ?? "active",
        logo_url: row.logo_url ?? null,
      },
    ];
  });
}

/** {@link listCompaniesHybrid} result: the lexical page, possibly extended. */
export interface HybridCompanyListResult extends CompanyListResult {
  /**
   * How many semantic-only rows were appended after the lexical matches.
   * 0 whenever the blend was skipped or added nothing; the page shows its
   * "includes semantic matches" hint only when this is positive.
   */
  semanticCount: number;
  /**
   * The lexical total on its own — what {@link listCompanies} would have
   * reported. The page keys the husk fallback on THIS being zero (a semantic
   * hit must not suppress the "we track X but have no profile yet" box,
   * which is about name matches).
   */
  lexicalTotal: number;
}

/**
 * listCompanies, plus semantic blending when a query embedding is available:
 * lexical (ilike) matches come first, in their normal order — a user typing
 * an exact or partial company name must see that name at the top — then
 * semantic-only neighbors (cosine-ranked via {@link semanticCompanySearch})
 * are appended, deduped by slug against the lexical rows, capped to the page
 * size. `total` counts the appended rows so "Showing X–Y of N" stays honest.
 *
 * The blend deliberately narrows to the one case where it is both useful and
 * honest, and falls back to plain listCompanies otherwise:
 *
 * - `queryEmbedding` null (embedder failed/timed out/absent — the state
 *   secret-free CI exercises): pure lexical, silently.
 * - Explicit sort (`opts.sort` set): appending cosine-ranked rows under
 *   "Name (A–Z)" or "Largest raise" would misorder the list — semantic
 *   reordering under an explicit sort is a lie. Callers pass `sort:
 *   undefined` for the default ordering (listCompanies orders name-asc
 *   either way).
 * - Page 2+ (`opts.offset` > 0): blending only on page 1 keeps pagination
 *   honest with zero bookkeeping — and since extras are only appended when
 *   the lexical page has room (lexical total < page size ⇒ exactly one
 *   lexical page), a blended result never coexists with real pagination.
 * - Any column filter active (industry/source/tag/state/the C2 VC filters):
 *   semantic_companies() ranks the WHOLE catalog (it applies only the
 *   exclusion + catalog-bar semantics), so appended extras could violate an
 *   explicit filter — e.g. non-Fintech rows under industry=Fintech. Blend
 *   only when q is the sole active constraint.
 * - Lexical page already full: nothing to append (implied by the above,
 *   checked anyway).
 *
 * The RPC runs concurrently with the lexical query — its result is discarded
 * in the rare full-page case rather than serializing the two round trips.
 */
export async function listCompaniesHybrid(
  opts: CompanyListOptions,
  queryEmbedding: number[] | null,
): Promise<HybridCompanyListResult> {
  const limit = opts.limit ?? 30;
  const columnFiltersActive =
    opts.industry_group != null ||
    opts.discovered_via != null ||
    opts.tag != null ||
    opts.state != null ||
    opts.min_raised != null ||
    opts.max_raised != null ||
    opts.founded_after != null ||
    opts.founded_before != null ||
    opts.emp_min != null ||
    opts.emp_max != null ||
    opts.stage != null ||
    opts.funded_since_days != null;
  const blendEligible =
    queryEmbedding !== null &&
    Boolean(opts.search) &&
    opts.sort == null &&
    (opts.offset ?? 0) === 0 &&
    !columnFiltersActive;

  const [lexical, semantic] = await Promise.all([
    listCompanies(opts),
    blendEligible && queryEmbedding !== null
      ? semanticCompanySearch(queryEmbedding)
      : Promise.resolve([]),
  ]);

  const base = {
    ...lexical,
    semanticCount: 0,
    lexicalTotal: lexical.total,
  };
  if (!blendEligible || lexical.rows.length >= limit) return base;

  const seen = new Set(lexical.rows.map((r) => r.slug));
  const extras = semantic
    .filter((r) => !seen.has(r.slug))
    .slice(0, limit - lexical.rows.length);
  if (extras.length === 0) return base;

  return {
    rows: [...lexical.rows, ...extras],
    total: lexical.total + extras.length,
    semanticCount: extras.length,
    lexicalTotal: lexical.total,
  };
}

/**
 * Minimum number of catalog companies an `industry_group` must apply to before
 * it appears in the /companies filter dropdown. The LLM emits ~227 distinct
 * groups, ~64% of which apply to a single company, so the unfiltered dropdown
 * was an unusable wall of near-duplicate singletons. Requiring ≥3 companies
 * keeps only industries that actually group the catalog. Mirrors the analogous
 * {@link MIN_TAG_COMPANY_COUNT} threshold for tags. Raise/lower in one place here.
 */
const MIN_INDUSTRY_COMPANY_COUNT = 3;

/**
 * Non-null `industry_group` values that apply to at least
 * {@link MIN_INDUSTRY_COMPANY_COUNT} catalog companies, sorted, for the index
 * filter dropdown. Tallied in-process from a full keyset scan via
 * {@link scanCompanies}: a flat select is silently capped at 1000 rows by
 * PostgREST (`.limit(5000)` does not override the server cap), which dropped
 * every group that only occurs outside that arbitrary unordered sample. Counting
 * over the whole catalog also lets us drop singleton/near-singleton groups so
 * the dropdown isn't dominated by one-company entries. The page renders an "All
 * industries" default option independently of this list, so trimming it never
 * removes the unfiltered choice. `discovered_via` is a small fixed enum, so the
 * page hardcodes those options rather than querying for them.
 */
export async function listIndustryGroups(): Promise<string[]> {
  const rows = await scanCompanies(
    "listIndustryGroups",
    "slug, industry_group",
    "industry_group",
    true,
  );
  if (rows === null) return [];

  const counts = new Map<string, number>();
  for (const row of rows) {
    const value = row.industry_group as string | null;
    if (value) counts.set(value, (counts.get(value) ?? 0) + 1);
  }

  return [...counts.entries()]
    .filter(([, count]) => count >= MIN_INDUSTRY_COMPANY_COUNT)
    .map(([value]) => value)
    .sort((a, b) => a.localeCompare(b));
}

/**
 * Distinct `discovered_via` values present in the catalog (excluding excluded
 * companies), for the source filter dropdown. Returns a sorted list so the
 * dropdown is deterministic. Falls back to [] when Supabase is unconfigured so
 * the page still builds during CI.
 *
 * Uses the same keyset-scan helper as listIndustryGroups — PostgREST caps
 * single-shot selects at 1000 rows, and we want the full catalog.
 */
export async function listDiscoveredViaValues(): Promise<string[]> {
  const rows = await scanCompanies(
    "listDiscoveredViaValues",
    "slug, discovered_via",
    "discovered_via",
    true,
  );
  if (rows === null) return [];

  const seen = new Set<string>();
  for (const row of rows) {
    const value = row.discovered_via as string | null;
    if (value) seen.add(value);
  }
  return [...seen].sort((a, b) => a.localeCompare(b));
}

/**
 * Husk fallback search (Task 1.5): when the main catalog search returns 0
 * results for a non-empty term, run a second query that includes companies with
 * no description (husks) that match the name. Used to surface well-known
 * companies (Anthropic, Vercel, etc.) that haven't been enriched yet.
 *
 * Husks are companies where `exclusion_reason IS NULL` but which fail the
 * catalog bar (`description_short IS NULL AND funding_round_count = 0`). We
 * can't simply invert CATALOG_BAR_OR because PostgREST doesn't expose NOT (…OR…)
 * natively via the JS client without raw RPC. Instead we query
 * `exclusion_reason IS NULL AND name ILIKE %term%` without the catalog bar,
 * limited to ~10, and return only rows not already in the main results (which is
 * an empty array in this fallback path).
 */
export async function searchHuskFallback(
  term: string,
): Promise<HuskFallbackRow[]> {
  const supabase = supabaseOrNull("searchHuskFallback");
  if (!supabase) return [];

  const safe = sanitizeIlikeTerm(term);
  if (!safe) return [];

  const { data, error } = await supabase
    .from("companies")
    .select("slug, name")
    .is("exclusion_reason", null)
    .ilike("name", `%${safe}%`)
    .order("name", { ascending: true })
    .limit(10);

  if (error) {
    console.error("[searchHuskFallback] query failed:", error.message);
    return [];
  }

  return ((data ?? []) as { slug: string | null; name: string | null }[])
    .filter((r): r is { slug: string; name: string } => r.slug != null && r.name != null);
}

// ─── Front-page queries ───────────────────────────────────────────────────────

/** One "Recent fundings" margin-note row on the front page. */
export interface RecentFundingRow {
  companySlug: string;
  companyName: string;
  round_type: string | null;
  amount_raised: number | null;
  announced_date: string;
}

// Nested shape from funding_rounds → companies(name, slug).
interface NestedFundingCompany {
  name: string | null;
  slug: string | null;
}

// Raw join rows returned by the recent-funding / recent-news selects (global
// firehose + the entity-scoped variants). Shared so every feed query maps
// through one place and the row → RssItem-input shaping can never drift.
interface RawRecentFundingRow {
  round_type: string | null;
  amount_raised: number | null;
  announced_date: string;
  companies: NestedFundingCompany | NestedFundingCompany[] | null;
}

interface RawRecentNewsRow {
  id: string | null;
  title: string | null;
  url: string | null;
  source: string | null;
  published_date: string | null;
  companies: NestedFundingCompany | NestedFundingCompany[] | null;
}

/** Flatten recent-funding join rows, dropping any whose company join is missing. */
function mapRecentFundingRows(rows: RawRecentFundingRow[]): RecentFundingRow[] {
  return rows.flatMap((row) => {
    const company = Array.isArray(row.companies)
      ? row.companies[0]
      : row.companies;
    if (!company?.name || !company.slug) return [];
    return [
      {
        companySlug: company.slug,
        companyName: company.name,
        round_type: row.round_type,
        amount_raised: row.amount_raised,
        announced_date: row.announced_date,
      },
    ];
  });
}

/** Flatten recent-news join rows, dropping incomplete rows / missing joins. */
function mapRecentNewsRows(rows: RawRecentNewsRow[]): RecentNewsRow[] {
  return rows.flatMap((row) => {
    const company = Array.isArray(row.companies)
      ? row.companies[0]
      : row.companies;
    if (!row.id || !row.title || !row.url || !company?.name || !company.slug) {
      return [];
    }
    return [
      {
        id: row.id,
        title: row.title,
        url: row.url,
        source: row.source ?? "",
        published_date: row.published_date,
        companySlug: company.slug,
        companyName: company.name,
      },
    ];
  });
}

/**
 * The latest funding rounds with a known announce date, newest first, joined
 * with the company's name and slug. Rows whose company join is missing are
 * dropped (every fact on the page must link somewhere).
 */
export async function listRecentFundings(
  limit = 5,
): Promise<RecentFundingRow[]> {
  const supabase = supabaseOrNull("listRecentFundings");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("funding_rounds")
    .select("round_type, amount_raised, announced_date, companies!inner(name, slug)")
    .is("companies.exclusion_reason", null)
    .not("announced_date", "is", null)
    .order("announced_date", { ascending: false })
    .limit(limit);

  if (error) {
    console.error("[listRecentFundings] query failed:", error.message);
    return [];
  }

  return mapRecentFundingRows((data ?? []) as RawRecentFundingRow[]);
}

/** One recent news article, joined to its company — for the RSS feed. */
export interface RecentNewsRow {
  id: string;
  title: string;
  url: string;
  source: string;
  published_date: string | null;
  companySlug: string;
  companyName: string;
}

/**
 * The latest news articles across the catalog with a known publish date, newest
 * first, joined to a non-excluded company. Feeds the /feed.xml RSS document
 * (paired with {@link listRecentFundings}). Excluded companies' articles never
 * surface; rows whose company join is missing are dropped. Returns [] on
 * missing env or error.
 */
export async function listRecentNews(limit = 30): Promise<RecentNewsRow[]> {
  const supabase = supabaseOrNull("listRecentNews");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("news_articles")
    .select("id, title, url, source, published_date, companies!inner(name, slug)")
    .is("companies.exclusion_reason", null)
    .not("published_date", "is", null)
    .order("published_date", { ascending: false })
    .limit(limit);

  if (error) {
    console.error("[listRecentNews] query failed:", error.message);
    return [];
  }

  return mapRecentNewsRows((data ?? []) as RawRecentNewsRow[]);
}

// ─── Entity-scoped feed queries (per-entity RSS, ROADMAP Next #3) ──────────────
//
// Fan-outs of the global firehose (listRecentFundings / listRecentNews) scoped
// to one industry_group or to a set of company slugs (an investor's portfolio).
// Each mirrors its global sibling exactly — same tables, same shown-cohort
// filter (companies.exclusion_reason IS NULL) via the inner join, same
// dated-only gate + newest-first order — plus one scoping filter. They feed the
// per-entity RSS route handlers and reuse the shared row mappers above.

/** Max company slugs an `.in(...)` feed filter accepts, bounding the request
 * URL length. Portfolios above this are truncated (the feed still shows the 40
 * most recent events across the first N companies) — see the investor route. */
export const FEED_IN_SLUGS_CAP = 150;

/**
 * Recent funding rounds for companies in one canonical `industry_group`, newest
 * first. Mirrors {@link listRecentFundings} + an `industry_group` filter on the
 * inner-joined company. Returns [] on missing env or error.
 */
export async function listRecentFundingsByIndustry(
  group: string,
  limit = 30,
): Promise<RecentFundingRow[]> {
  const supabase = supabaseOrNull("listRecentFundingsByIndustry");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("funding_rounds")
    .select("round_type, amount_raised, announced_date, companies!inner(name, slug)")
    .is("companies.exclusion_reason", null)
    .eq("companies.industry_group", group)
    .not("announced_date", "is", null)
    .order("announced_date", { ascending: false })
    .limit(limit);

  if (error) {
    console.error("[listRecentFundingsByIndustry] query failed:", error.message);
    return [];
  }

  return mapRecentFundingRows((data ?? []) as RawRecentFundingRow[]);
}

/**
 * Recent news for companies in one canonical `industry_group`, newest first.
 * Mirrors {@link listRecentNews} + an `industry_group` filter on the
 * inner-joined company. Returns [] on missing env or error.
 */
export async function listRecentNewsByIndustry(
  group: string,
  limit = 30,
): Promise<RecentNewsRow[]> {
  const supabase = supabaseOrNull("listRecentNewsByIndustry");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("news_articles")
    .select("id, title, url, source, published_date, companies!inner(name, slug)")
    .is("companies.exclusion_reason", null)
    .eq("companies.industry_group", group)
    .not("published_date", "is", null)
    .order("published_date", { ascending: false })
    .limit(limit);

  if (error) {
    console.error("[listRecentNewsByIndustry] query failed:", error.message);
    return [];
  }

  return mapRecentNewsRows((data ?? []) as RawRecentNewsRow[]);
}

/**
 * Recent funding rounds for a specific set of company slugs (an investor's
 * portfolio), newest first. Mirrors {@link listRecentFundings} + an
 * `.in("companies.slug", slugs)` filter on the inner-joined company. Empty slug
 * list short-circuits to [] (never issues an `in.()` query). Returns [] on
 * missing env or error.
 */
export async function listRecentFundingsForCompanySlugs(
  slugs: string[],
  limit = 30,
): Promise<RecentFundingRow[]> {
  if (slugs.length === 0) return [];
  const supabase = supabaseOrNull("listRecentFundingsForCompanySlugs");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("funding_rounds")
    .select("round_type, amount_raised, announced_date, companies!inner(name, slug)")
    .is("companies.exclusion_reason", null)
    .in("companies.slug", slugs)
    .not("announced_date", "is", null)
    .order("announced_date", { ascending: false })
    .limit(limit);

  if (error) {
    console.error(
      "[listRecentFundingsForCompanySlugs] query failed:",
      error.message,
    );
    return [];
  }

  return mapRecentFundingRows((data ?? []) as RawRecentFundingRow[]);
}

/**
 * Recent news for a specific set of company slugs (an investor's portfolio),
 * newest first. Mirrors {@link listRecentNews} + an `.in("companies.slug",
 * slugs)` filter on the inner-joined company. Empty slug list short-circuits to
 * [] (never issues an `in.()` query). Returns [] on missing env or error.
 */
export async function listRecentNewsForCompanySlugs(
  slugs: string[],
  limit = 30,
): Promise<RecentNewsRow[]> {
  if (slugs.length === 0) return [];
  const supabase = supabaseOrNull("listRecentNewsForCompanySlugs");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("news_articles")
    .select("id, title, url, source, published_date, companies!inner(name, slug)")
    .is("companies.exclusion_reason", null)
    .in("companies.slug", slugs)
    .not("published_date", "is", null)
    .order("published_date", { ascending: false })
    .limit(limit);

  if (error) {
    console.error(
      "[listRecentNewsForCompanySlugs] query failed:",
      error.message,
    );
    return [];
  }

  return mapRecentNewsRows((data ?? []) as RawRecentNewsRow[]);
}

/** One "Biggest recent rounds" row on /trends. */
export interface BiggestRoundRow {
  companySlug: string;
  companyName: string;
  round_type: string | null;
  amount_raised: number;
  announced_date: string;
}

/**
 * The largest funding rounds announced in the last `sinceDays` days, biggest
 * first, joined to the company — the /trends "biggest recent rounds" board.
 * Only dated, amounted rounds for non-excluded companies (excluded companies'
 * rounds never surface, matching every other funding surface).
 *
 * De-duplicated on (company, round_type, amount): the historical news backfill
 * could re-report ONE round from several articles (Helion's $465M Series G was
 * stored 5×; see lib/funding.ts), which would otherwise fill the board with
 * copies of a single mega-round. Same per-company key as
 * {@link dedupedRoundsTotal}, extended with the company slug since this query
 * spans all companies — so two genuinely distinct rounds a company raised in
 * the window still both show, and two companies' equal-sized rounds never merge.
 * Over-fetches then trims to `limit` after the dedup. Returns [] on missing env
 * or error.
 */
export async function listBiggestRecentRounds(
  limit = 10,
  sinceDays = 180,
): Promise<BiggestRoundRow[]> {
  const supabase = supabaseOrNull("listBiggestRecentRounds");
  if (!supabase) return [];

  const cutoff = new Date(Date.now() - sinceDays * 86400e3)
    .toISOString()
    .slice(0, 10); // announced_date is a DATE column (YYYY-MM-DD).

  const { data, error } = await supabase
    .from("funding_rounds")
    .select("round_type, amount_raised, announced_date, companies!inner(name, slug)")
    .is("companies.exclusion_reason", null)
    .not("announced_date", "is", null)
    .not("amount_raised", "is", null)
    .gte("announced_date", cutoff)
    .order("amount_raised", { ascending: false })
    .limit(limit * 4);

  if (error) {
    console.error("[listBiggestRecentRounds] query failed:", error.message);
    return [];
  }

  type Row = {
    round_type: string | null;
    amount_raised: number | string | null;
    announced_date: string;
    companies: NestedFundingCompany | NestedFundingCompany[] | null;
  };

  const seen = new Set<string>();
  const rounds: BiggestRoundRow[] = [];
  for (const row of (data ?? []) as Row[]) {
    if (row.amount_raised == null) continue;
    const company = Array.isArray(row.companies)
      ? row.companies[0]
      : row.companies;
    if (!company?.name || !company.slug) continue;
    const key = `${company.slug}::${row.round_type ?? ""}::${row.amount_raised}`;
    if (seen.has(key)) continue;
    seen.add(key);
    rounds.push({
      companySlug: company.slug,
      companyName: company.name,
      round_type: row.round_type,
      amount_raised: Number(row.amount_raised),
      announced_date: row.announced_date,
    });
    if (rounds.length >= limit) break;
  }
  return rounds;
}

/** One "New on nous" margin-note row on the front page. */
export interface NewCompanyRow {
  slug: string;
  name: string;
  description_short: string | null;
}

/**
 * Newest companies by created_at, preferring ones with a one-liner and
 * falling back to name-only rows to fill the requested count (spec §2).
 */
export async function listNewestCompanies(limit = 4): Promise<NewCompanyRow[]> {
  const supabase = supabaseOrNull("listNewestCompanies");
  if (!supabase) return [];

  // Over-fetch so described companies can be preferred without a second query.
  const { data, error } = await supabase
    .from("companies")
    .select("slug, name, description_short")
    .is("exclusion_reason", null)
    .or(CATALOG_BAR_OR)
    .order("created_at", { ascending: false })
    .limit(limit * 3);

  if (error) {
    console.error("[listNewestCompanies] query failed:", error.message);
    return [];
  }

  const rows = (data ?? []) as NewCompanyRow[];
  const described = rows.filter((r) => r.description_short);
  const nameOnly = rows.filter((r) => !r.description_short);
  return [...described, ...nameOnly].slice(0, limit);
}

/** Top industry groups by company count, plus how many groups were left out. */
export interface IndustrySummary {
  top: string[];
  moreCount: number;
}

/**
 * Count industry_group frequencies in-process from a full keyset scan via
 * {@link scanCompanies} (same fetch as listIndustryGroups) and return the top
 * N. Ranking over the whole catalog — not the first 1000 rows PostgREST
 * happens to return — keeps the top-N and the "+N more" count accurate and
 * deterministic across ISR revalidations.
 */
export async function getIndustrySummary(topN = 6): Promise<IndustrySummary> {
  const rows = await scanCompanies(
    "getIndustrySummary",
    "slug, industry_group",
    "industry_group",
    true,
  );
  if (rows === null) return { top: [], moreCount: 0 };

  const counts = new Map<string, number>();
  for (const row of rows) {
    const value = row.industry_group as string | null;
    if (value) counts.set(value, (counts.get(value) ?? 0) + 1);
  }

  const top = [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, topN)
    .map(([value]) => value);

  return { top, moreCount: Math.max(0, counts.size - top.length) };
}

// ─── Industry landing pages (0036 momentum RPCs) ──────────────────────────────

/** One `industry_group` bucket with its exact catalog head-count. */
export interface IndustryCount {
  group: string;
  count: number;
}

/**
 * Canonical `industry_group` buckets — those applying to at least
 * {@link MIN_INDUSTRY_COMPANY_COUNT} catalog companies — with head-counts,
 * ranked by count desc then name. Same full-catalog keyset tally as
 * {@link listIndustryGroups} (a flat select is silently capped at 1000 rows by
 * PostgREST). `/industry`, its detail routes, and the sitemap gate to EXACTLY
 * this list, so an arbitrary freeform label can never mint a thin page.
 */
export async function listCanonicalIndustries(): Promise<IndustryCount[]> {
  const rows = await scanCompanies(
    "listCanonicalIndustries",
    "slug, industry_group",
    "industry_group",
    true,
  );
  if (rows === null) return [];

  const counts = new Map<string, number>();
  for (const row of rows) {
    const value = row.industry_group as string | null;
    if (value) counts.set(value, (counts.get(value) ?? 0) + 1);
  }

  const ranked = [...counts.entries()]
    .filter(([, count]) => count >= MIN_INDUSTRY_COMPANY_COUNT)
    .map(([group, count]) => ({ group, count }))
    .sort((a, b) => b.count - a.count || a.group.localeCompare(b.group));

  // Guard against two labels that slugify to the same URL ("AI/ML" vs "AI ML"
  // both → "ai-ml"): keep only the first (highest-count) label per slug, so the
  // index, the detail route's resolver, and the sitemap all agree on exactly one
  // reachable page per slug. Collisions are rare (enrichment labels are
  // consistent) but this makes a stray one impossible to turn into a silently
  // unreachable page (both index rows would otherwise link to one URL, and the
  // resolver would render only the first). Map preserves the ranked order.
  const bySlug = new Map<string, IndustryCount>();
  for (const entry of ranked) {
    const slug = industryToSlug(entry.group);
    if (!bySlug.has(slug)) bySlug.set(slug, entry);
  }
  return [...bySlug.values()];
}

// ─── Market map (/map/[industry]) ─────────────────────────────────────────────

/** One positioned market-map node: coords + the fields the SVG needs. */
export interface MapCompanyNode {
  slug: string;
  name: string;
  map_x: number;
  map_y: number;
  /** Node radius (sqrt-scaled); null → min radius. */
  latest_round_amount: number | null;
  /** Optional node coloring (deferred in v1; carried for a follow-up). */
  primary_category: string | null;
}

/**
 * Cap on nodes rendered per map. A market map with >~400 dots is unreadable and
 * the SVG payload balloons; we take the most prominent by latest raise. Also
 * dodges PostgREST's silent 1000-row cap without a keyset scan (most industries
 * are well under it, but this makes the bound explicit and the SVG bounded).
 */
const MAP_NODE_LIMIT = 400;

/**
 * Companies in one canonical industry that have precomputed map coordinates,
 * ranked by latest raise desc (biggest first — the nodes we most want to keep
 * and label), name asc as a stable tiebreak, capped to {@link MAP_NODE_LIMIT}.
 *
 * Filters mirror every other public surface: exclusion_reason IS NULL + the
 * catalog bar. Selecting map_x/map_y EXPLICITLY means that until the columns
 * reach prod (migration ordering) this 400s → the standard error path → [] →
 * the page's empty state. That is the intended "no coords yet" behavior, so no
 * feature flag is needed (same property {@link getCompanyOgData} relies on).
 * Returns [] on missing env or error.
 */
export async function listIndustryMapNodes(
  group: string,
  limit = MAP_NODE_LIMIT,
): Promise<MapCompanyNode[]> {
  const supabase = supabaseOrNull("listIndustryMapNodes");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("companies")
    .select("slug, name, map_x, map_y, latest_round_amount, primary_category")
    .is("exclusion_reason", null)
    .or(CATALOG_BAR_OR)
    .eq("industry_group", group)
    .not("map_x", "is", null)
    .not("map_y", "is", null)
    .order("latest_round_amount", { ascending: false, nullsFirst: false })
    .order("name", { ascending: true })
    .limit(limit);

  if (error) {
    console.error("[listIndustryMapNodes] query failed:", error.message);
    return [];
  }

  return (
    (data ?? []) as unknown as {
      slug: string | null;
      name: string | null;
      map_x: number | null;
      map_y: number | null;
      latest_round_amount: number | null;
      primary_category: string | null;
    }[]
  ).flatMap((r) =>
    r.slug && r.name && r.map_x != null && r.map_y != null
      ? [
          {
            slug: r.slug,
            name: r.name,
            map_x: r.map_x,
            map_y: r.map_y,
            latest_round_amount: r.latest_round_amount ?? null,
            primary_category: r.primary_category ?? null,
          },
        ]
      : [],
  );
}

/**
 * Minimum mapped companies before an industry earns a /map page. A 3-dot map is
 * thin; mirrors {@link MIN_INDUSTRY_COMPANY_COUNT}'s spirit for the map surface.
 */
const MIN_MAP_NODE_COUNT = 8;

/**
 * Canonical industry labels that have ≥ {@link MIN_MAP_NODE_COUNT} companies
 * with precomputed coords — the gate for the /map hub and the sitemap map URLs.
 * Same full-catalog keyset tally as {@link listCanonicalIndustries} (a flat
 * select caps at 1000 rows). Returns [] when the map_x column is absent on prod
 * (scanCompanies → null on the 400) — so maps enter the hub/sitemap ONLY once
 * coords exist, never indexing an empty map. Sorted by count desc then name.
 */
export async function listIndustriesWithMapCoords(): Promise<string[]> {
  const rows = await scanCompanies(
    "listIndustriesWithMapCoords",
    "slug, industry_group, map_x",
    "map_x", // notNullColumn — server-side drops null-coord rows
    true, // catalogOnly
  );
  if (rows === null) return [];

  const counts = new Map<string, number>();
  for (const row of rows) {
    const g = row.industry_group as string | null;
    if (g) counts.set(g, (counts.get(g) ?? 0) + 1);
  }
  return [...counts.entries()]
    .filter(([, c]) => c >= MIN_MAP_NODE_COUNT)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([g]) => g);
}

// ─── Momentum / "heating up" (/trending, migration 0039) ──────────────────────

/**
 * Minimum momentum_score to surface on /trending. A floor, not the badge
 * threshold (see MOMENTUM_BADGE_THRESHOLD). The pipeline's score is in [0,1]
 * (0.5 = flat, higher = accelerating), so 0 lists every scored company; raise
 * it once the distribution is known to hide flat/neutral rows.
 */
const MIN_MOMENTUM_SCORE = 0;

/**
 * The highest-momentum shown companies for /trending, momentum_score desc.
 * Mirrors {@link listIndustryMapNodes} exactly: the same public-surface filters
 * (exclusion_reason IS NULL + the catalog bar), the same `.not(col,"is",null)`
 * gate so only SCORED companies surface, and the same explicit-select
 * degradation. Selecting momentum_score/momentum_computed_at/momentum_why
 * EXPLICITLY means that until the columns reach prod (migration ordering) this
 * 400s → the standard error path → [] → the page's empty state — the intended
 * "no scores yet" behavior, no feature flag needed (the identical property
 * {@link getCompanyOgData} / {@link listIndustryMapNodes} rely on). When the
 * migration lands, scored rows appear on the next ISR revalidation. Returns []
 * on missing env or error.
 */
export async function listHeatingUpCompanies(
  limit = 30,
  minScore = MIN_MOMENTUM_SCORE,
): Promise<MomentumCompany[]> {
  const supabase = supabaseOrNull("listHeatingUpCompanies");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("companies")
    .select(
      "slug, name, hq_city, hq_state, industry_group, description_short, status, logo_url, momentum_score, momentum_computed_at, momentum_why",
    )
    .is("exclusion_reason", null)
    .or(CATALOG_BAR_OR)
    .not("momentum_score", "is", null)
    .gte("momentum_score", minScore)
    .order("momentum_score", { ascending: false, nullsFirst: false })
    .order("name", { ascending: true })
    .limit(limit);

  if (error) {
    console.error("[listHeatingUpCompanies] query failed:", error.message);
    return [];
  }

  return (
    (data ?? []) as unknown as {
      slug: string | null;
      name: string | null;
      hq_city: string | null;
      hq_state: string | null;
      industry_group: string | null;
      description_short: string | null;
      status: string | null;
      logo_url: string | null;
      momentum_score: number | null;
      momentum_computed_at: string | null;
      momentum_why: string[] | null;
    }[]
  ).flatMap((r) =>
    r.slug && r.name && r.momentum_score != null
      ? [
          {
            slug: r.slug,
            name: r.name,
            hq_city: r.hq_city ?? null,
            hq_state: r.hq_state ?? null,
            industry_group: r.industry_group ?? null,
            description_short: r.description_short ?? null,
            status: r.status ?? "active",
            logo_url: r.logo_url ?? null,
            momentumScore: Number(r.momentum_score),
            momentumComputedAt: r.momentum_computed_at ?? null,
            momentumWhy: Array.isArray(r.momentum_why) ? r.momentum_why : [],
          },
        ]
      : [],
  );
}

/**
 * The `funding_by_quarter` RPC (migration 0036): pre-aggregated quarter totals,
 * oldest first, over the last `quarters` calendar quarters (including the
 * in-progress one). `industryGroup` scopes it to one bucket for the per-industry
 * chart, or null (the default) sums the whole catalog for the /trends chart.
 * The aggregation lives in SQL because a flat select of every round would blow
 * PostgREST's 1000-row cap on the largest industries. Returns [] on missing env
 * or error — the chart degrades to its empty state, never breaks the page.
 */
export async function fundingByQuarter(
  quarters: number,
  industryGroup?: string,
): Promise<QuarterTotal[]> {
  const supabase = supabaseOrNull("fundingByQuarter");
  if (!supabase) return [];

  const { data, error } = await supabase.rpc("funding_by_quarter", {
    p_quarters: quarters,
    p_industry_group: industryGroup ?? null,
  });

  if (error) {
    console.error("[fundingByQuarter] rpc failed:", error.message);
    return [];
  }

  return (
    (data ?? []) as { quarter_start: string | null; total_usd: number | string | null }[]
  ).flatMap((r) =>
    r.quarter_start
      ? [{ quarter_start: r.quarter_start, total_usd: r.total_usd }]
      : [],
  );
}

/** One row of the `industry_funding_momentum` RPC (0036). */
export interface IndustryMomentumRow {
  industry_group: string;
  /** Raised in the trailing 2 complete quarters, USD. */
  recent_usd: number;
  /** Raised in the 2 quarters before that, USD. */
  prior_usd: number;
  /** Round count in the recent window. */
  round_count: number;
}

/**
 * The `industry_funding_momentum` RPC (migration 0036): per-industry
 * trailing-2-complete-quarter raised (recent) vs the 2 quarters before (prior)
 * — the same window math as the themes growth metric, with the in-progress
 * quarter excluded so a mid-quarter run never compares a partial window against
 * full ones. The web derives growth = (recent − prior)/prior itself (see
 * {@link fundingGrowth}) and ranks the "hottest industries". Returns [] on
 * missing env or error.
 */
export async function industryFundingMomentum(): Promise<IndustryMomentumRow[]> {
  const supabase = supabaseOrNull("industryFundingMomentum");
  if (!supabase) return [];

  const { data, error } = await supabase.rpc("industry_funding_momentum", {});

  if (error) {
    console.error("[industryFundingMomentum] rpc failed:", error.message);
    return [];
  }

  return (
    (data ?? []) as {
      industry_group: string | null;
      recent_usd: number | string | null;
      prior_usd: number | string | null;
      round_count: number | null;
    }[]
  ).flatMap((r) =>
    r.industry_group
      ? [
          {
            industry_group: r.industry_group,
            recent_usd: Number(r.recent_usd ?? 0),
            prior_usd: Number(r.prior_usd ?? 0),
            round_count: r.round_count ?? 0,
          },
        ]
      : [],
  );
}

/** Exact number of companies in the index (head-only count). */
export async function countCompanies(): Promise<number> {
  const supabase = supabaseOrNull("countCompanies");
  if (!supabase) return 0;

  const { count, error } = await supabase
    .from("companies")
    .select("id", { count: "exact", head: true })
    .is("exclusion_reason", null)
    .or(CATALOG_BAR_OR);

  if (error) {
    console.error("[countCompanies] query failed:", error.message);
    return 0;
  }
  return count ?? 0;
}

/**
 * Slug of one uniformly random company, for /surprise: exact count, then a
 * single row at a random offset (ordered so the offset is stable within a
 * request). Returns null when the index is empty.
 */
export async function getRandomCompanySlug(): Promise<string | null> {
  const supabase = supabaseOrNull("getRandomCompanySlug");
  if (!supabase) return null;

  const { count, error: countError } = await supabase
    .from("companies")
    .select("id", { count: "exact", head: true })
    .is("exclusion_reason", null)
    .or(CATALOG_BAR_OR);

  if (countError || !count) {
    if (countError) {
      console.error(
        "[getRandomCompanySlug] count failed:",
        countError.message,
      );
    }
    return null;
  }

  const offset = Math.floor(Math.random() * count);
  const { data, error } = await supabase
    .from("companies")
    .select("slug")
    .is("exclusion_reason", null)
    .or(CATALOG_BAR_OR)
    .order("id", { ascending: true })
    .range(offset, offset);

  if (error) {
    console.error("[getRandomCompanySlug] slug fetch failed:", error.message);
    return null;
  }
  return (data?.[0]?.slug as string | undefined) ?? null;
}

// ─── SEO queries ──────────────────────────────────────────────────────────────

/** Minimal per-company row for the sitemap. */
export interface CompanySlugRow {
  slug: string;
  updated_at: string | null;
}

/** Result of a {@link scanTable} walk. */
interface TableScanResult {
  rows: Record<string, unknown>[];
  /**
   * False when Supabase was unconfigured or a page failed mid-scan — `rows`
   * then holds only the pages fetched before the failure, and each caller
   * decides whether that partial result is usable. Hitting the `maxPages`
   * bound is NOT an error: the scan warns loudly and returns ok with what it
   * has.
   */
  ok: boolean;
}

/**
 * Keyset-paginated full scan of a slug-keyed table, shared by the sitemap,
 * tag/location, industry, and investor queries. PostgREST caps every response
 * at 1000 rows regardless of `.limit()`, and the company catalog already holds
 * ~4,200 rows, so any single-shot select silently truncates. This walks the
 * table ordered by `slug` (unique in both scanned tables, so the cursor
 * strictly advances) in 1000-row pages via `.gt("slug", cursor)` until a short
 * page. Keyset beats offset `.range()` here: termination is provable, and rows
 * inserted mid-iteration can't shift offsets and cause skips or duplicates. A
 * hard `maxPages` bound caps the walk at 50k rows — also Google's per-sitemap
 * URL cap, so sitemap callers must split into multiple sitemaps before this
 * cap may be raised — and guarantees a pathological loop can never hang the
 * build; hitting the bound warns loudly instead of truncating silently.
 *
 * `select` must include `slug` (the cursor column). When `notNullColumn` is
 * given, rows where that column is null are filtered out server-side. Errors
 * are logged under `label`.
 */
async function scanTable(
  table: "companies" | "investors",
  label: string,
  select: string,
  notNullColumn?: string,
  catalogOnly = false,
): Promise<TableScanResult> {
  const supabase = supabaseOrNull(label);
  if (!supabase) return { rows: [], ok: false };

  const pageSize = 1000;
  const maxPages = 50; // 50k rows — Google's per-sitemap URL cap.
  const all: Record<string, unknown>[] = [];
  let lastSlug: string | null = null;

  for (let page = 0; page < maxPages; page++) {
    let query = supabase
      .from(table)
      .select(select)
      .order("slug", { ascending: true })
      .limit(pageSize);
    if (notNullColumn !== undefined) {
      query = query.not(notNullColumn, "is", null);
    }
    if (catalogOnly) {
      query = query.is("exclusion_reason", null).or(CATALOG_BAR_OR);
    }
    if (lastSlug !== null) {
      query = query.gt("slug", lastSlug);
    }

    const { data, error } = await query;

    if (error) {
      console.error(`[${label}] page query failed:`, error.message);
      return { rows: all, ok: false };
    }

    // supabase-js types `.select()` results by parsing the literal column
    // string; with a runtime `string` it falls back to GenericStringError, so
    // widen through unknown — callers narrow per-column as elsewhere in this file.
    const rows = (data ?? []) as unknown as Record<string, unknown>[];
    all.push(...rows);

    // A short (or empty) page means we've drained the table.
    if (rows.length < pageSize) return { rows: all, ok: true };

    lastSlug = rows[rows.length - 1].slug as string;
  }

  console.warn(
    `[${label}] hit maxPages=${maxPages} (${all.length} rows); ` +
      "results may be truncated — see scanTable's doc before raising the cap.",
  );
  return { rows: all, ok: true };
}

/**
 * {@link scanTable} over `companies`, with the all-or-nothing error shape the
 * company callers rely on: null when Supabase is unconfigured or any page
 * fails (partial pages are discarded) so callers can fall back to their empty
 * value.
 */
async function scanCompanies(
  label: string,
  select: string,
  notNullColumn?: string,
  catalogOnly = false,
): Promise<Record<string, unknown>[] | null> {
  const { rows, ok } = await scanTable(
    "companies",
    label,
    select,
    notNullColumn,
    catalogOnly,
  );
  return ok ? rows : null;
}

/**
 * Every company slug + updated_at, for app/sitemap.ts. Keyset-paginated via
 * {@link scanCompanies} — see its doc for why a flat select would truncate.
 * Returns [] on error or missing env — CI builds without Supabase secrets and
 * the sitemap must still build with just its static entries.
 */
export async function listAllCompanySlugs(): Promise<CompanySlugRow[]> {
  const rows = await scanCompanies(
    "listAllCompanySlugs",
    "slug, updated_at",
    undefined,
    true,
  );
  if (rows === null) return [];
  return rows.map((r) => ({
    slug: r.slug as string,
    updated_at: (r.updated_at as string | null) ?? null,
  }));
}

/** The handful of fields the company OG-image card renders. */
export interface CompanyOgData {
  name: string;
  industry_group: string | null;
  /**
   * Hybrid total in USD: max(article-stated cumulative total, sum of known
   * round amounts) — same display rule as the detail-page tile, minus the
   * attribution text (no room on the card). 0 when nothing is known.
   */
  totalRaised: number;
}

/**
 * Lean fetch for app/c/[slug]/opengraph-image.tsx — deliberately not
 * getCompanyBySlug, which fans out into five queries the card doesn't need.
 * One query: the company row (including the stated total_raised_* columns)
 * with its rounds' types+amounts embedded (`funding_rounds(round_type, amount_raised)`);
 * max(stated, sum) computed in-process.
 * Returns null when the slug is unknown (caller falls back to the site card).
 * Missing/empty rounds degrade to totalRaised = 0 — the card still renders,
 * just without the raised line. Note: until migration 0021 reaches prod this
 * explicit select 400s (unknown column), which lands on the same error path →
 * site-card fallback; the route still never throws.
 */
export async function getCompanyOgData(
  slug: string,
): Promise<CompanyOgData | null> {
  const supabase = supabaseOrNull("getCompanyOgData");
  if (!supabase) return null;

  const { data: company, error: companyError } = await supabase
    .from("companies")
    .select(
      "name, industry_group, exclusion_reason, total_raised_usd, funding_rounds(round_type, amount_raised)",
    )
    .eq("slug", slug)
    .single();

  if (companyError || !company) {
    if (companyError?.code !== "PGRST116") {
      console.error(
        "[getCompanyOgData] company query failed:",
        companyError?.message,
      );
    }
    return null;
  }

  if ((company as { exclusion_reason?: string | null }).exclusion_reason) {
    return null;
  }

  // Runtime-built select string → supabase-js can't parse the columns, so
  // narrow through a local row shape (same idiom as scanTable).
  const row = company as unknown as {
    name: string;
    industry_group: string | null;
    total_raised_usd: number | null;
    funding_rounds:
      | { round_type: string | null; amount_raised: number | null }[]
      | { round_type: string | null; amount_raised: number | null }
      | null;
  };

  // One-to-many embed: PostgREST returns an array, but guard null/object
  // shapes so a missing join degrades to 0 instead of breaking the card.
  const roundsRaw = row.funding_rounds;
  const rounds = Array.isArray(roundsRaw)
    ? roundsRaw
    : roundsRaw != null
      ? [roundsRaw]
      : [];

  return {
    name: row.name,
    industry_group: row.industry_group ?? null,
    totalRaised: computeTotalRaised(row.total_raised_usd, rounds).total,
  };
}

/**
 * Survivor slug for a dead (merged-away) company slug, or null when no alias
 * exists. Dedup merges DELETE the loser row but record its slug in
 * slug_aliases (migration 0032) so inbound links keep working: the /c/[slug]
 * and /alternatives/[slug] pages consult this ONLY on their miss path — a live
 * slug resolves via its own query first, so valid pages pay zero extra
 * queries and a live slug always shadows a stale alias.
 *
 * One query: the alias row keyed by old_slug with the survivor's CURRENT slug
 * embedded through the company_id FK (aliases store the id, not a slug copy,
 * so later merges/renames can't leave a redirect pointing at a second dead
 * slug). Returns null on missing env, unknown slug (PGRST116), or a dangling
 * embed — callers then fall through to notFound() exactly as before. Until
 * migration 0032 reaches prod, the select 400s (unknown table) and lands on
 * the same null path, so the pages degrade to today's plain 404.
 */
export async function getAliasTargetSlug(slug: string): Promise<string | null> {
  const supabase = supabaseOrNull("getAliasTargetSlug");
  if (!supabase) return null;

  const { data, error } = await supabase
    .from("slug_aliases")
    .select("companies!company_id(slug)")
    .eq("old_slug", slug)
    .single();

  if (error || !data) {
    if (error?.code !== "PGRST116") {
      // PGRST116 = "no rows" — the expected miss; anything else is unexpected.
      console.error("[getAliasTargetSlug] query failed:", error?.message);
    }
    return null;
  }

  // PostgREST may hand the embed back as an object or a single-element array;
  // normalize (same idiom as every other embed in this file).
  const row = data as unknown as {
    companies: { slug: string | null } | { slug: string | null }[] | null;
  };
  const company = Array.isArray(row.companies)
    ? row.companies[0]
    : row.companies;
  return company?.slug ?? null;
}

/**
 * Return the full detail for a single company identified by slug.
 * Returns null when the slug does not exist.
 *
 * Three queries:
 *   1. companies — the main row.
 *   2. funding_rounds — with nested investor joins.
 *   3. competitors — with the resolved competitor company, when matched.
 */
export async function getCompanyBySlug(
  slug: string,
): Promise<CompanyDetail | null> {
  const supabase = supabaseOrNull("getCompanyBySlug");
  if (!supabase) return null;

  // 1. Fetch company row.
  const { data: company, error: companyError } = await supabase
    .from("companies")
    .select("*")
    .eq("slug", slug)
    .single();

  if (companyError || !company) {
    if (companyError?.code !== "PGRST116") {
      // PGRST116 = "no rows" — anything else is unexpected
      console.error(
        "[getCompanyBySlug] company query failed:",
        companyError?.message,
      );
    }
    return null;
  }

  // Excluded companies 404 (spec 2026-06-12) — junk pages must not render
  // even by direct URL. Hidden-but-legit husks (no exclusion) still render.
  if ((company as { exclusion_reason?: string | null }).exclusion_reason) {
    return null;
  }

  const companyId = company.id as string;

  // 2, 3 & 4: fetch people, funding rounds (with nested investor joins), and
  // competitors (with resolved company) in parallel.
  const [
    peopleResult,
    roundsResult,
    competitorsResult,
    investorsResult,
    newsResult,
    verificationsResult,
  ] = await Promise.all([
      supabase
        .from("people")
        .select("*")
        .eq("company_id", companyId)
        .order("rank", { ascending: true }),

      supabase
        .from("funding_rounds")
        .select("*, funding_round_investors(is_lead, investors(name))")
        .eq("company_id", companyId),

      supabase
        .from("competitors")
        .select("*, competitor_company:companies!competitor_company_id(slug, name, exclusion_reason)")
        .eq("company_id", companyId)
        .order("rank", { ascending: true }),

      supabase
        .from("company_investors")
        .select("is_lead, source, investors(name, website)")
        .eq("company_id", companyId),

      supabase
        .from("news_articles")
        .select("id, url, title, source, published_date, funding_round_id")
        .eq("company_id", companyId)
        .order("published_date", { ascending: false, nullsFirst: false }),

      // Source-verification: only `supported` verdicts back the public "✓ Verified
      // against source" affordance. Migration-order-free — a missing table / any
      // error degrades to no badges (handled below), never a page failure.
      supabase
        .from("fact_verifications")
        .select("fact_kind, fact_ref, source_url, supporting_quote")
        .eq("company_id", companyId)
        .eq("verdict", "supported"),
    ]);

  if (peopleResult.error) {
    console.error(
      "[getCompanyBySlug] people query failed:",
      peopleResult.error.message,
    );
  }
  if (roundsResult.error) {
    console.error(
      "[getCompanyBySlug] funding_rounds query failed:",
      roundsResult.error.message,
    );
  }
  if (competitorsResult.error) {
    console.error(
      "[getCompanyBySlug] competitors query failed:",
      competitorsResult.error.message,
    );
  }
  if (investorsResult.error) {
    console.error(
      "[getCompanyBySlug] company_investors query failed:",
      investorsResult.error.message,
    );
  }
  if (newsResult.error) {
    console.error(
      "[getCompanyBySlug] news_articles query failed:",
      newsResult.error.message,
    );
  }

  const people = (peopleResult.data ?? []) as PersonRow[];

  const rawRounds = (roundsResult.data ?? []) as FundingRoundJoin[];

  // Shape funding rounds: split join rows into lead vs other investor names,
  // then sort by announced_date desc with nulls last.
  const fundingRounds: FundingRoundWithInvestors[] = rawRounds
    .map((round) => {
      const joinRows = round.funding_round_investors ?? [];
      const leadInvestors: string[] = [];
      const otherInvestors: string[] = [];

      for (const j of joinRows) {
        // PostgREST can return the related row as either an object or a
        // single-element array depending on join cardinality; normalize.
        const inv = Array.isArray(j.investors) ? j.investors[0] : j.investors;
        const name = inv?.name;
        if (!name) continue;
        if (j.is_lead === true) {
          leadInvestors.push(name);
        } else {
          otherInvestors.push(name);
        }
      }

      // Strip the nested join field from the returned object — the caller only
      // sees the flattened leadInvestors / otherInvestors arrays.
      const {
        funding_round_investors: _funding_round_investors,
        ...rest
      } = round;
      void _funding_round_investors;
      return { ...rest, leadInvestors, otherInvestors };
    })
    .sort((a, b) => {
      // Nulls last; otherwise ISO date string compare is lexicographically correct.
      if (a.announced_date === null && b.announced_date === null) return 0;
      if (a.announced_date === null) return 1;
      if (b.announced_date === null) return -1;
      return b.announced_date.localeCompare(a.announced_date);
    });

  const rawCompetitors = (competitorsResult.data ?? []) as CompetitorJoin[];
  const competitors: CompetitorWithResolved[] = rawCompetitors.map((row) => {
    const nested = Array.isArray(row.competitor_company)
      ? row.competitor_company[0]
      : row.competitor_company;
    const resolved =
      nested && nested.slug && nested.name && !nested.exclusion_reason
        ? { slug: nested.slug, name: nested.name }
        : null;
    const { competitor_company: _competitor_company, ...rest } = row;
    void _competitor_company;
    return { ...rest, resolved };
  });

  const investors: CompanyInvestorRow[] = (
    (investorsResult.data ?? []) as CompanyInvestorJoin[]
  ).flatMap((row) => {
    const inv = Array.isArray(row.investors) ? row.investors[0] : row.investors;
    if (!inv?.name) return [];
    return [
      {
        name: inv.name,
        website: inv.website ?? null,
        isLead: row.is_lead === true,
        source: row.source ?? "",
      },
    ];
  });

  const news = (newsResult.data ?? []) as NewsArticleRow[];

  if (verificationsResult.error) {
    // Table absent (pre-migration) or any other error → no ✓ badges; the page
    // still renders (migration-order-free, same posture as momentum/map).
    console.error(
      "[getCompanyBySlug] fact_verifications query failed:",
      verificationsResult.error.message,
    );
  }
  const verifications = (verificationsResult.data ?? []) as FactVerification[];

  return {
    company: company as unknown as CompanyRow,
    people,
    fundingRounds,
    competitors,
    investors,
    news,
    verifications,
  };
}

// ─── "Alternatives to X" pages (SEO) ──────────────────────────────────────────

/**
 * LLM scratch-notes occasionally leak into a competitor's stored rationale
 * (e.g. "Included temporarily for evaluation but should be dropped."). The
 * Competitors component drops such rows so internal model reasoning never
 * reaches a customer; the /alternatives page applies the SAME guard so a
 * leaked row can't appear there either — nor inflate the ≥3-competitor sitemap
 * threshold or the JSON-LD list. Keep this pattern in sync with the copy in
 * components/Competitors.tsx (the canonical display-side guard).
 */

/**
 * Shape returned by the nested competitor → resolved-company select used by
 * {@link getAlternatives}. Carries the full card projection (so resolved
 * alternatives render in a CompanyCard with their logo), plus exclusion_reason
 * so excluded companies are dropped (their /c/[slug] 404s).
 */
interface AlternativesResolvedCompany {
  slug: string | null;
  name: string | null;
  hq_city: string | null;
  hq_state: string | null;
  industry_group: string | null;
  description_short: string | null;
  status: string | null;
  logo_url: string | null;
  exclusion_reason?: string | null;
}

type AlternativesCompetitorJoin = CompetitorRow & {
  competitor_company:
    | AlternativesResolvedCompany
    | AlternativesResolvedCompany[]
    | null;
};

/**
 * Data for /alternatives/[slug] — "Top alternatives to {Company}". Returns the
 * subject company's display fields plus its competitors, split into:
 *   - `resolved`: competitors that matched an indexed company (rendered as
 *     linked CompanyCards, so they carry the full card projection + logo).
 *   - `named`:    LLM-named competitors with no nous page (name + reasoning).
 *
 * Mirrors the competitor fetch in {@link getCompanyBySlug} but selects the full
 * card projection on the resolved company. Both lists are ordered by competitor
 * `rank` ascending (1 = most relevant). Meta-leak rows are filtered out (see
 * {@link competitorLeaksMeta}). Returns null when the slug is unknown or the
 * company is excluded (the page then 404s) — note an *empty* competitor set is
 * still a non-null result, so the page can render a graceful "no alternatives
 * yet" state rather than a 404.
 */
export async function getAlternatives(
  slug: string,
): Promise<AlternativesData | null> {
  const supabase = supabaseOrNull("getAlternatives");
  if (!supabase) return null;

  // 1. Subject company — only the fields the page header + metadata need.
  const { data: company, error: companyError } = await supabase
    .from("companies")
    .select("id, slug, name, description_short, industry_group, exclusion_reason")
    .eq("slug", slug)
    .single();

  if (companyError || !company) {
    if (companyError?.code !== "PGRST116") {
      console.error(
        "[getAlternatives] company query failed:",
        companyError?.message,
      );
    }
    return null;
  }

  // Excluded companies 404 on /c/[slug]; their alternatives page must 404 too.
  if ((company as { exclusion_reason?: string | null }).exclusion_reason) {
    return null;
  }

  const companyId = company.id as string;

  // 2. Competitors with the resolved company's full card projection embedded.
  const { data: competitorRows, error: competitorsError } = await supabase
    .from("competitors")
    .select(
      "*, competitor_company:companies!competitor_company_id(slug, name, hq_city, hq_state, industry_group, description_short, status, logo_url, exclusion_reason)",
    )
    .eq("company_id", companyId)
    .order("rank", { ascending: true });

  if (competitorsError) {
    console.error(
      "[getAlternatives] competitors query failed:",
      competitorsError.message,
    );
    // A company with no readable competitors is still a valid (empty) page.
    return {
      company: {
        slug: company.slug as string,
        name: company.name as string,
        description_short: (company.description_short as string | null) ?? null,
        industry_group: (company.industry_group as string | null) ?? null,
      },
      resolved: [],
      named: [],
    };
  }

  const resolved: AlternativeCompany[] = [];
  const named: NamedAlternative[] = [];

  for (const row of (competitorRows ?? []) as unknown as AlternativesCompetitorJoin[]) {
    // Same display-side guard as the Competitors component: never surface a row
    // whose stored reasoning/description is leaked model scratch-text.
    if (
      competitorLeaksMeta(row)
    ) {
      continue;
    }

    const nested = Array.isArray(row.competitor_company)
      ? row.competitor_company[0]
      : row.competitor_company;

    // Resolved → linked card, but only when the matched company is itself
    // listable (has slug + name and isn't excluded). Otherwise fall back to the
    // text-only "named" treatment so we never render a dead /c/[slug] link.
    if (nested && nested.slug && nested.name && !nested.exclusion_reason) {
      resolved.push({
        slug: nested.slug,
        name: nested.name,
        hq_city: nested.hq_city ?? null,
        hq_state: nested.hq_state ?? null,
        industry_group: nested.industry_group ?? null,
        description_short: nested.description_short ?? null,
        status: nested.status ?? "active",
        logo_url: nested.logo_url ?? null,
        rank: row.rank,
        reasoning: row.reasoning ?? null,
        description: row.description ?? null,
        source: row.source,
        source_url: row.source_url ?? null,
      });
    } else {
      named.push({
        name: row.competitor_name,
        rank: row.rank,
        reasoning: row.reasoning ?? null,
        description: row.description ?? null,
        source: row.source,
        source_url: row.source_url ?? null,
      });
    }
  }

  return {
    company: {
      slug: company.slug as string,
      name: company.name as string,
      description_short: (company.description_short as string | null) ?? null,
      industry_group: (company.industry_group as string | null) ?? null,
    },
    resolved,
    named,
  };
}

// ─── Relationship graph (similar / also-backed-by) ────────────────────────────

/**
 * "Similar" companies for the relationship-graph section on /c/[slug]: the
 * directed `company_relationships` edges (company_id → related_company_id) of
 * type 'similar', joined with the related company's display fields, ranked by
 * score desc and capped at 12.
 *
 * The embedded company is narrowed through {@link NestedRelatedCompany} (never
 * `any`); PostgREST may hand the embed back as an object or a single-element
 * array, so both shapes are normalized — same idiom as the competitors join.
 * Rows whose related company didn't resolve (missing slug/name) are dropped.
 * Returns [] on missing env (build-time prerender / local dev without
 * .env.local), like every other helper here.
 */
export async function getRelatedCompanies(
  companyId: string,
): Promise<RelatedCompany[]> {
  const supabase = supabaseOrNull("getRelatedCompanies");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("company_relationships")
    .select(
      "score, evidence, related_company:companies!related_company_id(slug, name, description_short, status, industry_group, exclusion_reason)",
    )
    .eq("company_id", companyId)
    .eq("relationship_type", "similar")
    .order("score", { ascending: false })
    .limit(12);

  if (error) {
    console.error("[getRelatedCompanies] query failed:", error.message);
    return [];
  }

  return ((data ?? []) as CompanyRelationshipJoin[]).flatMap((row) => {
    const c = Array.isArray(row.related_company)
      ? row.related_company[0]
      : row.related_company;
    // Drop unresolved joins AND excluded companies — the latter 404 on
    // /c/[slug], so linking them produces dead "Related companies" links.
    if (!c?.slug || !c.name || c.exclusion_reason) return [];
    return [
      {
        slug: c.slug,
        name: c.name,
        descriptionShort: c.description_short ?? null,
        status: c.status ?? "active",
        industryGroup: c.industry_group ?? null,
        score: row.score != null ? Number(row.score) : 0,
        evidence: row.evidence ?? null,
      },
    ];
  });
}

// Nested prior-company shape from the career_moves → companies embed. career_moves
// has TWO FKs to companies (company_id + prior_company_id), so the embed MUST name
// the FK column (`companies!prior_company_id`) or PostgREST 400s "ambiguous".
interface NestedPriorCompany {
  slug: string | null;
  name: string | null;
  // Excluded prior companies 404 on /c/[slug]; carry the flag so the link is
  // dropped (the verbatim name still shows as text).
  exclusion_reason?: string | null;
}

type CareerMoveJoin = {
  person_name: string;
  prior_company_name: string;
  prior_role: string | null;
  start_year: number | null;
  end_year: number | null;
  prior_company: NestedPriorCompany | NestedPriorCompany[] | null;
};

/**
 * Founder background for /c/[slug]: each founder/exec and the companies they
 * worked at BEFORE this one, from the career_moves table (extract-career-history).
 * Grouped by person in the component.
 *
 * Migration-order-free: the explicit .select() 400s before career_moves (migration
 * 0040) reaches prod, and the error → [] degrade hides the section until the table
 * exists — then ISR revalidation surfaces it (same pattern as market-map / momentum,
 * no feature flag). The `companies!prior_company_id` FK hint is REQUIRED (two FKs to
 * companies). prior_company_id is nullable — most prior employers aren't catalogued,
 * so the name renders as plain text with no link.
 */
export async function getCareerMoves(companyId: string): Promise<CareerMove[]> {
  const supabase = supabaseOrNull("getCareerMoves");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("career_moves")
    .select(
      "person_name, prior_company_name, prior_role, start_year, end_year, prior_company:companies!prior_company_id(slug, name, exclusion_reason)",
    )
    .eq("company_id", companyId)
    .order("person_normalized_name", { ascending: true })
    .order("prior_company_name", { ascending: true });

  if (error) {
    console.error("[getCareerMoves] query failed:", error.message);
    return [];
  }

  return ((data ?? []) as CareerMoveJoin[]).map((row) => {
    const c = Array.isArray(row.prior_company)
      ? row.prior_company[0]
      : row.prior_company;
    // Link only to a SHOWN prior company; an excluded one 404s, so keep the
    // verbatim name as text and drop the link.
    const linkable = Boolean(c?.slug && c?.name && !c.exclusion_reason);
    return {
      personName: row.person_name,
      priorCompanyName: row.prior_company_name,
      priorRole: row.prior_role ?? null,
      startYear: row.start_year ?? null,
      endYear: row.end_year ?? null,
      priorCompanySlug: linkable ? (c?.slug ?? null) : null,
    };
  });
}

// Cap on embedding-based similar companies. Matches the 2-column card grid
// (3 rows) and the similar_companies() default; passed explicitly so the SQL
// default and the UI cap can't drift apart silently.
const SIMILAR_COMPANIES_LIMIT = 6;

// Shape returned by the similar_companies() Postgres function (migration
// 0033). Narrowed rather than `any`, same as the nested-select joins.
interface SimilarCompanyRpcRow {
  id: string | null;
  slug: string | null;
  name: string | null;
  logo_url: string | null;
  description_short: string | null;
  industry_group: string | null;
  similarity: number | null;
}

/**
 * Embedding-based similar companies for /c/[slug]: nearest neighbors by
 * cosine similarity over the pipeline-computed description embeddings
 * (companies.embedding, migration 0033).
 *
 * PostgREST cannot ORDER BY a vector distance through filter params, so the
 * ranking lives in the `similar_companies` SQL function and this helper calls
 * it via `.rpc()`. The function returns zero rows when the company has no
 * embedding yet (the section then falls back to the heuristic graph edges —
 * never fabricates) and already excludes the anchor company, excluded
 * companies, and unembedded rows; the slug/name guard here is the same
 * defense-in-depth used by getRelatedCompanies. Returns [] on missing env or
 * error, like every other helper in this file.
 */
export async function getSimilarCompanies(
  companyId: string,
): Promise<SimilarCompany[]> {
  const supabase = supabaseOrNull("getSimilarCompanies");
  if (!supabase) return [];

  const { data, error } = await supabase.rpc("similar_companies", {
    company_id: companyId,
    match_count: SIMILAR_COMPANIES_LIMIT,
  });

  if (error) {
    console.error("[getSimilarCompanies] rpc failed:", error.message);
    return [];
  }

  return ((data ?? []) as SimilarCompanyRpcRow[]).flatMap((row) => {
    if (!row.slug || !row.name) return [];
    return [
      {
        slug: row.slug,
        name: row.name,
        logoUrl: row.logo_url ?? null,
        descriptionShort: row.description_short ?? null,
        industryGroup: row.industry_group ?? null,
        similarity: row.similarity != null ? Number(row.similarity) : 0,
      },
    ];
  });
}

// An investor backing more than this many companies is treated as too
// high-degree to imply a meaningful relationship — a mega-fund like YC backs
// thousands, so including it would relate half the catalog. Such investors are
// dropped from the "also backed by" computation entirely.
const ALSO_BACKED_BY_MAX_INVESTOR_DEGREE = 30;
// Cap on the "also backed by" companies surfaced, ranked by shared-investor count.
const ALSO_BACKED_BY_LIMIT = 8;

/**
 * "Also backed by" companies for the relationship-graph section on /c/[slug]:
 * a two-hop shared-investor walk computed read-time, EXCLUDING high-degree
 * (mega-fund) investors so the result stays meaningful.
 *
 * The previous implementation only looked at `company_investors`, which in
 * practice points exclusively to ~13 mega-funds (all ≥52 holdings) that are
 * always filtered out by the degree cap. Boutique investors appear only in
 * `funding_round_investors`. This implementation UNIONs both paths in
 * TypeScript (PostgREST cannot UNION directly):
 *
 *   Step 1a: this company's investor_ids from `company_investors`.
 *   Step 1b: investor_ids from `funding_round_investors → funding_rounds`
 *            where funding_rounds.company_id = this company.
 *   (Union deduplicated in-process.)
 *
 *   Step 2: Each investor's total holding count across BOTH paths, merged
 *           in-process; drop investors with > ALSO_BACKED_BY_MAX_INVESTOR_DEGREE
 *           total distinct companies. Keep names of surviving low-degree investors.
 *
 *   Step 3: Other companies (≠ this) backed by any low-degree investor via
 *           EITHER path, tallied by shared-investor count; top ALSO_BACKED_BY_LIMIT.
 *
 *   Step 4: Resolve company ids to slug + name.
 *
 * Returns [] on missing env, any error, or when no low-degree investors exist.
 */
export async function getAlsoBackedBy(
  companyId: string,
): Promise<AlsoBackedByCompany[]> {
  const supabase = supabaseOrNull("getAlsoBackedBy");
  if (!supabase) return [];

  // Step 1: UNION company_investors + funding_round_investors for this company.
  // PostgREST has no UNION primitive — run both queries in parallel and merge.
  const [ciResult, friResult] = await Promise.all([
    // 1a: direct company-level investors.
    supabase
      .from("company_investors")
      .select("investor_id")
      .eq("company_id", companyId),

    // 1b: round-level investors — join through funding_rounds to get company_id.
    // PostgREST: select investor_id from funding_round_investors where
    //   funding_rounds.company_id = companyId.
    supabase
      .from("funding_round_investors")
      .select("investor_id, funding_rounds!inner(company_id)")
      .eq("funding_rounds.company_id", companyId),
  ]);

  if (ciResult.error) {
    console.error(
      "[getAlsoBackedBy] own company_investors query failed:",
      ciResult.error.message,
    );
    return [];
  }
  if (friResult.error) {
    console.error(
      "[getAlsoBackedBy] own funding_round_investors query failed:",
      friResult.error.message,
    );
    // Non-fatal: fall through with only the company_investors result.
  }

  // Deduplicate investor ids across both sources.
  const ownIdSet = new Set<string>();
  for (const r of (ciResult.data ?? []) as { investor_id: string | null }[]) {
    if (r.investor_id) ownIdSet.add(r.investor_id);
  }
  // friResult rows embed a funding_rounds object — the investor_id is flat.
  for (const r of ((friResult.data ?? []) as {
    investor_id: string | null;
    funding_rounds: { company_id: string } | { company_id: string }[] | null;
  }[])) {
    if (r.investor_id) ownIdSet.add(r.investor_id);
  }

  const ownInvestorIds = [...ownIdSet];
  if (ownInvestorIds.length === 0) return [];

  // Step 2: compute each investor's total degree across BOTH paths in parallel.
  // We count DISTINCT companies via company_investors PLUS DISTINCT companies
  // via funding_round_investors (inner-joined through funding_rounds).
  // Both counts are head-only to avoid pulling large result sets.
  const degreeResults = await Promise.all(
    ownInvestorIds.map(async (id) => {
      const [ciCount, friCount] = await Promise.all([
        supabase
          .from("company_investors")
          .select("company_id", { count: "exact", head: true })
          .eq("investor_id", id),
        supabase
          .from("funding_round_investors")
          .select("funding_rounds!inner(company_id)", { count: "exact", head: true })
          .eq("investor_id", id),
      ]);

      if (ciCount.error || friCount.error) {
        // Treat an unknown degree as high-degree (exclude) — safer than
        // accidentally relating half the catalog on a transient error.
        return { id, count: Number.POSITIVE_INFINITY };
      }
      // Sum both paths; this over-counts companies that appear in both paths
      // for the same investor, but that's acceptable for the degree guard
      // (it errs on the side of excluding rather than including mega-funds).
      return { id, count: (ciCount.count ?? 0) + (friCount.count ?? 0) };
    }),
  );

  const lowDegreeIds = degreeResults
    .filter((r) => r.count <= ALSO_BACKED_BY_MAX_INVESTOR_DEGREE)
    .map((r) => r.id);

  if (lowDegreeIds.length === 0) return [];

  // Names of the surviving low-degree investors, for the attribution caption.
  const { data: investorRows, error: investorError } = await supabase
    .from("investors")
    .select("id, name")
    .in("id", lowDegreeIds);

  if (investorError) {
    console.error(
      "[getAlsoBackedBy] investor names query failed:",
      investorError.message,
    );
    return [];
  }

  const investorName = new Map<string, string>();
  for (const r of (investorRows ?? []) as {
    id: string | null;
    name: string | null;
  }[]) {
    if (r.id && r.name) investorName.set(r.id, r.name);
  }

  // Step 3: other companies backed by any low-degree investor via EITHER path,
  // tallied by count of shared low-degree investors. Run both edge-table queries
  // in parallel and merge in-process; order by investor_id so a transient
  // PostgREST 1000-row cap truncates deterministically.
  const [ciSharedResult, friSharedResult] = await Promise.all([
    // 3a: company_investors path.
    supabase
      .from("company_investors")
      .select("company_id, investor_id")
      .in("investor_id", lowDegreeIds)
      .neq("company_id", companyId)
      .order("investor_id", { ascending: true }),

    // 3b: funding_round_investors path — embed funding_rounds to get company_id.
    supabase
      .from("funding_round_investors")
      .select("investor_id, funding_rounds!inner(company_id)")
      .in("investor_id", lowDegreeIds)
      .neq("funding_rounds.company_id", companyId)
      .order("investor_id", { ascending: true }),
  ]);

  if (ciSharedResult.error) {
    console.error(
      "[getAlsoBackedBy] shared company_investors query failed:",
      ciSharedResult.error.message,
    );
    return [];
  }
  if (friSharedResult.error) {
    console.error(
      "[getAlsoBackedBy] shared funding_round_investors query failed:",
      friSharedResult.error.message,
    );
    // Non-fatal: fall through with company_investors results only.
  }

  // company_id → set of shared low-degree investor names. A set ensures a
  // (company, investor) pair is never double-counted even if the company appears
  // in both paths.
  const sharedByCompany = new Map<string, Set<string>>();

  // 3a: company_investors rows — company_id is flat.
  for (const r of (ciSharedResult.data ?? []) as {
    company_id: string | null;
    investor_id: string | null;
  }[]) {
    if (!r.company_id || !r.investor_id) continue;
    const name = investorName.get(r.investor_id);
    if (!name) continue;
    let names = sharedByCompany.get(r.company_id);
    if (!names) {
      names = new Set<string>();
      sharedByCompany.set(r.company_id, names);
    }
    names.add(name);
  }

  // 3b: funding_round_investors rows — company_id is nested inside funding_rounds.
  for (const r of ((friSharedResult.data ?? []) as {
    investor_id: string | null;
    funding_rounds: { company_id: string | null } | { company_id: string | null }[] | null;
  }[])) {
    if (!r.investor_id) continue;
    const name = investorName.get(r.investor_id);
    if (!name) continue;

    const fr = Array.isArray(r.funding_rounds) ? r.funding_rounds[0] : r.funding_rounds;
    const cid = fr?.company_id;
    if (!cid || cid === companyId) continue;

    let names = sharedByCompany.get(cid);
    if (!names) {
      names = new Set<string>();
      sharedByCompany.set(cid, names);
    }
    names.add(name);
  }

  if (sharedByCompany.size === 0) return [];

  // Rank by shared-investor count desc; company_id as a deterministic tiebreak.
  const ranked = [...sharedByCompany.entries()]
    .sort((a, b) => b[1].size - a[1].size || a[0].localeCompare(b[0]))
    .slice(0, ALSO_BACKED_BY_LIMIT);

  // Step 4: resolve those company ids to slug + name. Drop unresolved joins
  // (every surfaced company must link somewhere), then re-apply the ranking
  // order the `.in()` result does not preserve.
  const topIds = ranked.map(([id]) => id);
  const { data: companyRows, error: companyError } = await supabase
    .from("companies")
    .select("id, slug, name, exclusion_reason")
    .in("id", topIds);

  if (companyError) {
    console.error(
      "[getAlsoBackedBy] companies query failed:",
      companyError.message,
    );
    return [];
  }

  const companyById = new Map<string, { slug: string; name: string }>();
  for (const r of (companyRows ?? []) as {
    id: string | null;
    slug: string | null;
    name: string | null;
    exclusion_reason?: string | null;
  }[]) {
    // Skip excluded companies — their /c/[slug] page 404s, so surfacing them
    // here would be a dead link.
    if (r.id && r.slug && r.name && !r.exclusion_reason) {
      companyById.set(r.id, { slug: r.slug, name: r.name });
    }
  }

  return ranked.flatMap(([id, names]) => {
    const c = companyById.get(id);
    if (!c) return [];
    return [
      {
        slug: c.slug,
        name: c.name,
        sharedInvestors: [...names].sort((a, b) =>
          a.localeCompare(b, "en-US", { sensitivity: "base" }),
        ),
      },
    ];
  });
}

// ─── Tag / location SEO queries ───────────────────────────────────────────────

/**
 * Minimum number of catalog companies a tag must apply to before it earns a
 * /tag/<tag> page (and a sitemap entry). Of the ~7,370 tags the LLM emits, the
 * overwhelming majority apply to a single company, so per-tag pages were thin,
 * near-duplicate SEO doorways. Requiring ≥3 companies keeps only tags that
 * actually group the catalog. Raise/lower in one place here.
 *
 * Exported so the /tag/[tag] route can noindex any tag page below this same bar
 * — keeping page-level indexability in lockstep with sitemap inclusion (a tag
 * too thin for the sitemap is also told noindex, and vice versa).
 */
export const MIN_TAG_COMPANY_COUNT = 3;

/**
 * Non-null tag values that apply to at least {@link MIN_TAG_COMPANY_COUNT}
 * catalog companies, sorted. Singleton/near-singleton tags are dropped so the
 * tag/sitemap surface isn't dominated by thin one-company pages.
 *
 * PostgREST has no native `unnest` (nor DISTINCT) and caps every response at
 * 1000 rows, so a flat select would silently sample ~1/4 of the ~4,200-row
 * catalog. Instead we keyset-scan the whole table via {@link scanCompanies},
 * then tally `tags` occurrences in-process and keep those meeting the
 * threshold. Each company contributes at most once per distinct tag (a tags
 * array shouldn't repeat a value, but dedupe per row so it can't inflate a
 * count past the bar on its own).
 */
export async function listAllTags(): Promise<string[]> {
  const rows = await scanCompanies("listAllTags", "slug, tags", "tags", true);
  if (rows === null) return [];

  const counts = new Map<string, number>();
  for (const row of rows) {
    const tags = row.tags as string[] | null;
    if (!Array.isArray(tags)) continue;
    // Per-row dedupe: count each distinct tag once per company.
    const distinct = new Set<string>();
    for (const t of tags) {
      if (t) distinct.add(t);
    }
    for (const t of distinct) {
      counts.set(t, (counts.get(t) ?? 0) + 1);
    }
  }

  return [...counts.entries()]
    .filter(([, count]) => count >= MIN_TAG_COMPANY_COUNT)
    .map(([tag]) => tag)
    .sort((a, b) => a.localeCompare(b));
}

/**
 * All distinct, non-null `hq_state` values, sorted. Same full keyset scan +
 * in-process dedup idiom as {@link listAllTags} — PostgREST's 1000-row
 * response cap means anything short of paging the whole table drops rows.
 */
export async function listAllStates(): Promise<string[]> {
  const rows = await scanCompanies(
    "listAllStates",
    "slug, hq_state",
    "hq_state",
    true,
  );
  if (rows === null) return [];

  const seen = new Set<string>();
  for (const row of rows) {
    const value = row.hq_state as string | null;
    if (value) seen.add(value);
  }
  return [...seen].sort((a, b) => a.localeCompare(b));
}

/**
 * Minimum number of competitor rows a company must carry before its
 * /alternatives/<slug> page earns a sitemap entry. Mirrors the analogous
 * {@link MIN_TAG_COMPANY_COUNT} tag threshold: a company with only one or two
 * competitors makes a thin "alternatives" page with little SEO value, so we
 * only list ones with enough alternatives to be a useful comparison landing
 * page. The page itself still renders for any company with ≥1 competitor (and
 * is reachable via the on-page link) — this bar only gates sitemap inclusion.
 * Raise/lower in one place here.
 */
const MIN_ALTERNATIVES_COMPETITOR_COUNT = 3;

/**
 * Keyset-paginated full scan of the `competitors` table, tallying competitor
 * rows per `company_id`. PostgREST caps every response at 1000 rows regardless
 * of `.limit()`, and the table has several rows per company, so a flat select
 * would silently truncate. We page ordered by the row `id` (the UUID PK, unique
 * so the cursor strictly advances) via `.gt("id", cursor)` until a short page,
 * exactly like {@link scanTable} does for slug-keyed tables — but `competitors`
 * is keyed on a non-slug column, so it needs its own walk. A hard page bound
 * caps the scan and warns rather than looping. Returns null on missing env or a
 * mid-scan failure so the sitemap caller falls back to omitting these entries.
 */
async function countCompetitorsByCompany(): Promise<Map<string, number> | null> {
  const supabase = supabaseOrNull("countCompetitorsByCompany");
  if (!supabase) return null;

  const pageSize = 1000;
  const maxPages = 200; // up to 200k competitor rows — far above current scale.
  const counts = new Map<string, number>();
  let lastId: string | null = null;

  for (let page = 0; page < maxPages; page++) {
    let query = supabase
      .from("competitors")
      .select("id, company_id")
      .order("id", { ascending: true })
      .limit(pageSize);
    if (lastId !== null) query = query.gt("id", lastId);

    const { data, error } = await query;
    if (error) {
      console.error(
        "[countCompetitorsByCompany] page query failed:",
        error.message,
      );
      return null;
    }

    const rows = (data ?? []) as { id: string | null; company_id: string | null }[];
    for (const r of rows) {
      if (r.company_id) counts.set(r.company_id, (counts.get(r.company_id) ?? 0) + 1);
    }

    if (rows.length < pageSize) return counts;
    lastId = rows[rows.length - 1].id as string;
  }

  console.warn(
    `[countCompetitorsByCompany] hit maxPages=${maxPages}; counts may be partial.`,
  );
  return counts;
}

/**
 * Slugs (+ updated_at) of listable companies that carry at least
 * {@link MIN_ALTERNATIVES_COMPETITOR_COUNT} competitor rows, for the
 * /alternatives/<slug> sitemap entries. Tallies competitor counts per company
 * via {@link countCompetitorsByCompany}, then resolves the qualifying company
 * ids to slugs — dropping excluded companies (their pages 404) and any failing
 * the catalog bar (consistent with every other sitemap surface). Returns [] on
 * missing env or any error so the sitemap still builds with its other entries.
 */
export async function listAlternativesCompanySlugs(): Promise<CompanySlugRow[]> {
  const counts = await countCompetitorsByCompany();
  if (counts === null) return [];

  const qualifyingIds = [...counts.entries()]
    .filter(([, n]) => n >= MIN_ALTERNATIVES_COMPETITOR_COUNT)
    .map(([id]) => id);
  if (qualifyingIds.length === 0) return [];

  const supabase = supabaseOrNull("listAlternativesCompanySlugs");
  if (!supabase) return [];

  // Resolve ids → slugs in chunks so a large `.in(...)` list stays well under
  // any URL/length limits, applying the same exclusion + catalog bar as the
  // rest of the sitemap.
  const CHUNK = 500;
  const out: CompanySlugRow[] = [];
  for (let i = 0; i < qualifyingIds.length; i += CHUNK) {
    const chunk = qualifyingIds.slice(i, i + CHUNK);
    const { data, error } = await supabase
      .from("companies")
      .select("slug, updated_at")
      .in("id", chunk)
      .is("exclusion_reason", null)
      .or(CATALOG_BAR_OR);

    if (error) {
      console.error(
        "[listAlternativesCompanySlugs] slug resolve failed:",
        error.message,
      );
      return [];
    }

    for (const r of (data ?? []) as {
      slug: string | null;
      updated_at: string | null;
    }[]) {
      if (r.slug) out.push({ slug: r.slug, updated_at: r.updated_at ?? null });
    }
  }

  // Deterministic order so the sitemap is stable across revalidations.
  out.sort((a, b) => a.slug.localeCompare(b.slug));
  return out;
}

// ─── "New this week" queries ──────────────────────────────────────────────────

/** One row in the new-companies feed. */
export interface NewThisWeekCompanyRow {
  slug: string;
  name: string;
  description_short: string | null;
  industry_group: string | null;
  created_at: string;
}

/** One row in the new-funding-rounds feed. */
export interface NewThisWeekFundingRow {
  round_type: string | null;
  amount_raised: number | null;
  announced_date: string | null;
  created_at: string;
  companySlug: string;
  companyName: string;
}

/** Counts of companies and rounds extracted in the last N days. */
export interface NewThisWeekCounts {
  companies: number;
  rounds: number;
}

/** Companies extracted (created_at) in the last `days` days, newest first. */
export async function listNewThisWeekCompanies(
  days = 7,
  cap = 200,
): Promise<NewThisWeekCompanyRow[]> {
  const supabase = supabaseOrNull("listNewThisWeekCompanies");
  if (!supabase) return [];

  const cutoff = new Date(Date.now() - days * 86400e3).toISOString();

  const { data, error } = await supabase
    .from("companies")
    .select("slug, name, description_short, industry_group, created_at")
    .is("exclusion_reason", null)
    .or(CATALOG_BAR_OR)
    .gte("created_at", cutoff)
    .order("created_at", { ascending: false })
    .limit(cap);

  if (error) {
    console.error("[listNewThisWeekCompanies] query failed:", error.message);
    return [];
  }

  return (data ?? []).map((row) => ({
    slug: row.slug as string,
    name: row.name as string,
    description_short: (row.description_short as string | null) ?? null,
    industry_group: (row.industry_group as string | null) ?? null,
    created_at: row.created_at as string,
  }));
}

/**
 * Funding rounds extracted (created_at) in the last `days` days, newest first.
 * Uses extraction time — NOT announced_date — as the honesty claim is
 * "extracted this week". Rows with a missing company join are dropped (spec
 * requires every fact to link to a company page).
 */
export async function listNewThisWeekFundingRounds(
  days = 7,
  cap = 200,
): Promise<NewThisWeekFundingRow[]> {
  const supabase = supabaseOrNull("listNewThisWeekFundingRounds");
  if (!supabase) return [];

  const cutoff = new Date(Date.now() - days * 86400e3).toISOString();

  const { data, error } = await supabase
    .from("funding_rounds")
    .select(
      "round_type, amount_raised, announced_date, created_at, companies!inner(slug, name)",
    )
    .is("companies.exclusion_reason", null)
    .gte("created_at", cutoff)
    .order("created_at", { ascending: false })
    .limit(cap);

  if (error) {
    console.error("[listNewThisWeekFundingRounds] query failed:", error.message);
    return [];
  }

  type Row = {
    round_type: string | null;
    amount_raised: number | null;
    announced_date: string | null;
    created_at: string;
    companies: NestedFundingCompany | NestedFundingCompany[] | null;
  };

  return ((data ?? []) as Row[]).flatMap((row) => {
    const company = Array.isArray(row.companies)
      ? row.companies[0]
      : row.companies;
    if (!company?.name || !company.slug) return [];
    return [
      {
        round_type: row.round_type,
        amount_raised: row.amount_raised,
        announced_date: row.announced_date,
        created_at: row.created_at,
        companySlug: company.slug,
        companyName: company.name,
      },
    ];
  });
}

/**
 * Head-only counts of companies and funding rounds extracted in the last 7
 * days. Used by the homepage aside to decide whether to render the summary
 * line. Returns {companies: 0, rounds: 0} on any error so the page degrades
 * gracefully.
 */
export async function countNewThisWeek(days = 7): Promise<NewThisWeekCounts> {
  const supabase = supabaseOrNull("countNewThisWeek");
  if (!supabase) return { companies: 0, rounds: 0 };

  const cutoff = new Date(Date.now() - days * 86400e3).toISOString();

  const [companiesResult, roundsResult] = await Promise.all([
    supabase
      .from("companies")
      .select("id", { count: "exact", head: true })
      .is("exclusion_reason", null)
      .or(CATALOG_BAR_OR)
      .gte("created_at", cutoff),
    supabase
      // Inner-join companies + exclusion filter so this count matches what the
      // /new feed actually lists. listNewThisWeekFundingRounds drops rounds for
      // excluded/missing companies; without the same filter here the summary
      // line overstates the rounds shown below it ("76 extracted" vs 70 listed).
      .from("funding_rounds")
      .select("id, companies!inner(exclusion_reason)", { count: "exact", head: true })
      .is("companies.exclusion_reason", null)
      .gte("created_at", cutoff),
  ]);

  if (companiesResult.error) {
    console.error(
      "[countNewThisWeek] companies count failed:",
      companiesResult.error.message,
    );
  }
  if (roundsResult.error) {
    console.error(
      "[countNewThisWeek] rounds count failed:",
      roundsResult.error.message,
    );
  }

  return {
    companies: companiesResult.count ?? 0,
    rounds: roundsResult.count ?? 0,
  };
}

// ─── Investor pages ───────────────────────────────────────────────────────────

/** Paged result for the /investors index: rows for this page + total firm count. */
export interface InvestorListResult {
  rows: InvestorListRow[];
  total: number;
}

/**
 * A page of investors ranked by portfolio size (most holdings first), plus the
 * total investor count for pagination.
 *
 * Ranking uses the denormalized `portfolio_count` column (migration 0025), which
 * counts DISTINCT non-excluded companies a firm backs via EITHER `company_investors`
 * OR `funding_round_investors → funding_rounds`. This replaces the previous
 * embedded-aggregate ordering (`.order("count", { referencedTable })`) which
 * supabase-js silently ignores, causing the index to render alphabetically
 * instead of by portfolio size. `name` is the deterministic tiebreaker so paging
 * is stable when many firms share a count. `count: "exact"` returns the
 * unfiltered total in the same round trip.
 */
export async function listInvestors(
  opts: { limit?: number; offset?: number } = {},
): Promise<InvestorListResult> {
  const limit = opts.limit ?? 30;
  const offset = opts.offset ?? 0;

  const supabase = supabaseOrNull("listInvestors");
  if (!supabase) return { rows: [], total: 0 };

  const { data, error, count } = await supabase
    .from("investors")
    .select("slug, name, type, portfolio_count", { count: "exact" })
    .order("portfolio_count", { ascending: false })
    .order("name", { ascending: true })
    .range(offset, offset + limit - 1);

  if (error) {
    console.error("[listInvestors] query failed:", error.message);
    return { rows: [], total: 0 };
  }

  type Row = {
    slug: string | null;
    name: string | null;
    type: string | null;
    portfolio_count: number | null;
  };

  const rows = ((data ?? []) as Row[]).flatMap((r) => {
    if (!r.slug || !r.name) return [];
    return [
      {
        slug: r.slug,
        name: r.name,
        type: r.type ?? "unknown",
        portfolioCount: r.portfolio_count ?? 0,
      },
    ];
  });

  return { rows, total: count ?? rows.length };
}

/** Pagination window for the portfolio card list returned by getInvestorBySlug. */
export interface InvestorPortfolioPage {
  /** Max portfolio cards to return. Omit (or undefined) to return the full union. */
  limit?: number;
  /** Cards to skip from the front of the sorted union. Defaults to 0. */
  offset?: number;
}

/**
 * Full detail for a single investor by slug, or null when the slug is unknown.
 *
 * Three queries:
 *   1. investors — the firm row (id, display fields).
 *   2. company_investors → companies — the portfolio, shaped for CompanyCard.
 *   3. funding_round_investors → funding_rounds → companies — rounds this firm
 *      led or participated in, flattened with the funded company.
 *
 * The full portfolio union (company-level links + round-only companies, both
 * excluding excluded companies) is assembled and sorted in memory exactly as
 * before; `portfolioTotal` reports its length and `portfolio` is the slice for
 * the requested `opts` window. Passing no `opts` returns the entire union (so
 * the argument-free `generateMetadata` call is unaffected). The funding-activity
 * and round sections are not paginated — `rounds` is always the full list.
 */
export async function getInvestorBySlug(
  slug: string,
  opts: InvestorPortfolioPage = {},
): Promise<InvestorDetail | null> {
  const supabase = supabaseOrNull("getInvestorBySlug");
  if (!supabase) return null;

  // 1. The investor row. `portfolio_count` is the denormalized count from
  // migration 0025 — used in the header to match the /investors index.
  const { data: investor, error: investorError } = await supabase
    .from("investors")
    .select("id, slug, name, type, description, website, portfolio_count")
    .eq("slug", slug)
    .single();

  if (investorError || !investor) {
    if (investorError?.code !== "PGRST116") {
      console.error(
        "[getInvestorBySlug] investor query failed:",
        investorError?.message,
      );
    }
    return null;
  }

  const investorId = investor.id as string;

  // 2 & 3 in parallel: portfolio companies + rounds led/participated.
  const [portfolioResult, roundsResult] = await Promise.all([
    supabase
      .from("company_investors")
      .select(
        "companies(slug, name, hq_city, hq_state, industry_group, description_short, status, logo_url, exclusion_reason)",
      )
      .eq("investor_id", investorId),

    supabase
      .from("funding_round_investors")
      .select(
        "is_lead, funding_rounds(id, round_type, amount_raised, announced_date, primary_news_url, companies(slug, name, exclusion_reason))",
      )
      .eq("investor_id", investorId),
  ]);

  if (portfolioResult.error) {
    console.error(
      "[getInvestorBySlug] portfolio query failed:",
      portfolioResult.error.message,
    );
  }
  if (roundsResult.error) {
    console.error(
      "[getInvestorBySlug] rounds query failed:",
      roundsResult.error.message,
    );
  }

  // ── Portfolio: flatten the nested company, drop unresolved joins, sort by name.
  type PortfolioCompany = {
    slug: string | null;
    name: string | null;
    hq_city: string | null;
    hq_state: string | null;
    industry_group: string | null;
    description_short: string | null;
    status: string | null;
    logo_url?: string | null;
    exclusion_reason?: string | null;
  };
  type PortfolioJoin = {
    companies: PortfolioCompany | PortfolioCompany[] | null;
  };

  const portfolio: CompanyListRow[] = ((portfolioResult.data ?? []) as PortfolioJoin[])
    .flatMap((row) => {
      const c = Array.isArray(row.companies) ? row.companies[0] : row.companies;
      if (!c?.slug || !c.name || c.exclusion_reason) return [];
      return [
        {
          slug: c.slug,
          name: c.name,
          hq_city: c.hq_city ?? null,
          hq_state: c.hq_state ?? null,
          industry_group: c.industry_group ?? null,
          description_short: c.description_short ?? null,
          status: c.status ?? "active",
          logo_url: c.logo_url ?? null,
        },
      ];
    })
    .sort((a, b) => a.name.localeCompare(b.name, "en-US", { sensitivity: "base" }));

  // ── Rounds: flatten round + funded company, drop unresolved joins, newest first.
  type RoundJoin = {
    is_lead: boolean | null;
    funding_rounds:
      | {
          id: string;
          round_type: string | null;
          amount_raised: number | null;
          announced_date: string | null;
          primary_news_url: string | null;
          companies:
            | { slug: string | null; name: string | null; exclusion_reason?: string | null }
            | { slug: string | null; name: string | null; exclusion_reason?: string | null }[]
            | null;
        }
      | {
          id: string;
          round_type: string | null;
          amount_raised: number | null;
          announced_date: string | null;
          primary_news_url: string | null;
          companies:
            | { slug: string | null; name: string | null; exclusion_reason?: string | null }
            | { slug: string | null; name: string | null; exclusion_reason?: string | null }[]
            | null;
        }[]
      | null;
  };

  const rounds: InvestorRoundRow[] = ((roundsResult.data ?? []) as RoundJoin[])
    .flatMap((row) => {
      const fr = Array.isArray(row.funding_rounds)
        ? row.funding_rounds[0]
        : row.funding_rounds;
      if (!fr) return [];
      const c = Array.isArray(fr.companies) ? fr.companies[0] : fr.companies;
      if (!c?.slug || !c.name || c.exclusion_reason) return [];
      return [
        {
          roundId: fr.id,
          isLead: row.is_lead === true,
          round_type: fr.round_type,
          amount_raised: fr.amount_raised,
          announced_date: fr.announced_date,
          primary_news_url: fr.primary_news_url,
          companySlug: c.slug,
          companyName: c.name,
        },
      ];
    })
    .sort((a, b) => {
      // Newest first; nulls last. ISO date strings compare lexicographically.
      if (a.announced_date === null && b.announced_date === null) return 0;
      if (a.announced_date === null) return 1;
      if (b.announced_date === null) return -1;
      return b.announced_date.localeCompare(a.announced_date);
    });

  // Union the company-level portfolio with companies this investor funded via
  // rounds, so the rendered card list reflects every connected company and the
  // "Portfolio" section never reads "none" while Funding activity lists rounds.
  // ("Backs N" uses the denormalized portfolio_count, which covers both paths.)
  const haveSlugs = new Set(portfolio.map((c) => c.slug));
  const roundOnlySlugs = [
    ...new Set(rounds.map((r) => r.companySlug).filter((s) => !haveSlugs.has(s))),
  ];
  if (roundOnlySlugs.length > 0) {
    const { data: extra, error: extraError } = await supabase
      .from("companies")
      .select(
        "slug, name, hq_city, hq_state, industry_group, description_short, status, logo_url, exclusion_reason",
      )
      .in("slug", roundOnlySlugs);
    if (extraError) {
      console.error(
        "[getInvestorBySlug] round-company hydrate failed:",
        extraError.message,
      );
    }
    for (const c of (extra ?? []) as {
      slug: string | null;
      name: string | null;
      hq_city: string | null;
      hq_state: string | null;
      industry_group: string | null;
      description_short: string | null;
      status: string | null;
      logo_url?: string | null;
      exclusion_reason?: string | null;
    }[]) {
      if (!c.slug || !c.name || c.exclusion_reason) continue;
      portfolio.push({
        slug: c.slug,
        name: c.name,
        hq_city: c.hq_city ?? null,
        hq_state: c.hq_state ?? null,
        industry_group: c.industry_group ?? null,
        description_short: c.description_short ?? null,
        status: c.status ?? "active",
        logo_url: c.logo_url ?? null,
      });
    }
    portfolio.sort((a, b) =>
      a.name.localeCompare(b.name, "en-US", { sensitivity: "base" }),
    );
  }

  // `portfolio` is now the FULL deduplicated union (company-level + round-only),
  // already sorted by name. Paginate it in memory: portfolioTotal is the full
  // length the page pages over, `portfolio` is just the requested slice. The
  // data is small (card fields only) so slicing here is cheaper and keeps the
  // exclusion/union logic identical to before. With no opts, the slice is the
  // whole array (offset 0, no limit) — preserving the legacy full-list return.
  const portfolioTotal = portfolio.length;
  const offset = Math.max(0, opts.offset ?? 0);
  const portfolioPage =
    opts.limit === undefined
      ? portfolio.slice(offset)
      : portfolio.slice(offset, offset + Math.max(0, opts.limit));

  return {
    slug: investor.slug as string,
    name: investor.name as string,
    type: (investor.type as string | null) ?? "unknown",
    description: (investor.description as string | null) ?? null,
    website: (investor.website as string | null) ?? null,
    // portfolio_count is the denormalized total from migration 0025 (covers
    // both company_investors AND funding_round_investors paths). Use it as the
    // headline "Backs N companies" number so it matches the /investors index.
    // The paginated `portfolio` card list (and portfolioTotal) reflects the
    // resolved union; this headline count may be larger when some backed
    // companies aren't yet resolvable to a card. See Task 3.1.
    portfolioCount: (investor.portfolio_count as number | null) ?? portfolioTotal,
    portfolio: portfolioPage,
    portfolioTotal,
    rounds,
  };
}

/**
 * Every investor's display name → slug, for linking investor pills on company
 * pages. Full keyset scan via {@link scanTable} — see its doc for why a flat
 * select would truncate. Keyed on the lowercased name so the company page can
 * resolve a pill's display name regardless of casing. A mid-scan page failure
 * keeps the map built from the pages that did load (some linked pills beat
 * none); missing env yields {} so company pages still render (pills just stay
 * plain text).
 */
export async function getInvestorNameToSlugMap(): Promise<
  Record<string, string>
> {
  const { rows } = await scanTable(
    "investors",
    "getInvestorNameToSlugMap",
    "slug, name",
  );

  const map: Record<string, string> = {};
  for (const row of rows) {
    const slug = row.slug as string | null;
    const name = row.name as string | null;
    if (name && slug) {
      // First write wins on a casing collision — names are near-unique post
      // canonicalization, so this is just defensive.
      const key = name.trim().toLowerCase();
      if (!(key in map)) map[key] = slug;
    }
  }
  return map;
}

/**
 * Every investor slug + updated_at, for app/sitemap.ts. Full keyset scan via
 * {@link scanTable} — see its doc for why a flat select would truncate. A
 * mid-scan page failure keeps the rows from the pages that did load (a partial
 * sitemap beats an empty one); returns [] on missing env — CI builds without
 * Supabase secrets and the sitemap must still build with just its static
 * entries.
 */
export async function listAllInvestorSlugs(): Promise<InvestorSlugRow[]> {
  const { rows } = await scanTable(
    "investors",
    "listAllInvestorSlugs",
    "slug, updated_at",
  );
  return rows.map((r) => ({
    slug: r.slug as string,
    updated_at: (r.updated_at as string | null) ?? null,
  }));
}

/** Exact number of investors in the index (head-only count). */
export async function countInvestors(): Promise<number> {
  const supabase = supabaseOrNull("countInvestors");
  if (!supabase) return 0;

  const { count, error } = await supabase
    .from("investors")
    .select("id", { count: "exact", head: true });

  if (error) {
    console.error("[countInvestors] query failed:", error.message);
    return 0;
  }
  return count ?? 0;
}

// ─── Coverage / freshness (Task B2) ───────────────────────────────────────────

/**
 * Catalog-coverage summary for the /about page honesty line:
 * - `shown`       — listed companies (not excluded AND passing the catalog bar).
 * - `withFunding` — of those, how many carry ≥1 recorded funding round
 *                   (`funding_round_count > 0`).
 * - `asOf`        — the most recent `updated_at` across listed companies (the
 *                   freshest the catalog has been touched), or null if unknown.
 *
 * `shown`/`withFunding` are head-only exact counts (no rows pulled). `asOf` is a
 * single ordered row. Returns zeros + null on missing env or any error so the
 * page still renders (the line is simply suppressed when `shown` is 0).
 *
 * Note `withFunding` re-applies the same `exclusion_reason IS NULL` + catalog
 * bar as `shown`, then ANDs `funding_round_count.gt.0`. The catalog bar is
 * itself an `.or(description_short.not.is.null, funding_round_count.gt.0)`, so
 * any company with a funding round already passes the bar — the extra
 * `funding_round_count.gt.0` just narrows the count to the funded subset.
 */
export interface CoverageStats {
  shown: number;
  withFunding: number;
  asOf: string | null;
}

export async function getCoverageStats(): Promise<CoverageStats> {
  const supabase = supabaseOrNull("getCoverageStats");
  if (!supabase) return { shown: 0, withFunding: 0, asOf: null };

  const [shownResult, fundedResult, asOfResult] = await Promise.all([
    supabase
      .from("companies")
      .select("id", { count: "exact", head: true })
      .is("exclusion_reason", null)
      .or(CATALOG_BAR_OR),
    supabase
      .from("companies")
      .select("id", { count: "exact", head: true })
      .is("exclusion_reason", null)
      .or(CATALOG_BAR_OR)
      .gt("funding_round_count", 0),
    supabase
      .from("companies")
      .select("updated_at")
      .is("exclusion_reason", null)
      .or(CATALOG_BAR_OR)
      .not("updated_at", "is", null)
      .order("updated_at", { ascending: false })
      .limit(1),
  ]);

  if (shownResult.error) {
    console.error(
      "[getCoverageStats] shown count failed:",
      shownResult.error.message,
    );
  }
  if (fundedResult.error) {
    console.error(
      "[getCoverageStats] funded count failed:",
      fundedResult.error.message,
    );
  }
  if (asOfResult.error) {
    console.error(
      "[getCoverageStats] asOf query failed:",
      asOfResult.error.message,
    );
  }

  const asOfRow = (asOfResult.data ?? [])[0] as
    | { updated_at: string | null }
    | undefined;

  return {
    shown: shownResult.count ?? 0,
    withFunding: fundedResult.count ?? 0,
    asOf: asOfRow?.updated_at ?? null,
  };
}

/** One compact card in the homepage "Trending now" strip. */
export interface TrendingCompany {
  slug: string;
  name: string;
  oneLiner: string;
  facts: string[];
}

/**
 * Companies trending right now for the homepage strip. Reuses the existing
 * spotlight scoring ({@link buildSpotlightPool}) rather than inventing a second
 * ranking: that pool is already funding-gated (every entry has ≥1 round) and
 * scored by funding recency + amount + recent news volume + freshness, which is
 * exactly the "most recent rounds / highest recent news volume" signal this
 * strip wants.
 *
 * The spotlight deck consumes one entry at a time at the same UTC-day seed, so
 * taking the first `limit` of the (already shuffled) pool gives a stable strip
 * that rotates daily in lockstep with the deck, without re-querying. Returns []
 * on missing env / empty pool — the homepage renders nothing in that case.
 */
export async function getTrendingCompanies(
  limit = 6,
): Promise<TrendingCompany[]> {
  const pool: Spotlight[] = await buildSpotlightPool();
  return pool.slice(0, Math.max(0, limit)).map((s) => ({
    slug: s.slug,
    name: s.name,
    oneLiner: s.oneLiner,
    facts: s.facts,
  }));
}

// ─── Compare view (Task C5) ───────────────────────────────────────────────────

// Caps on how many names each compare cell lists, to keep the table readable.
const COMPARE_MAX_INVESTORS = 8;
const COMPARE_MAX_COMPETITORS = 6;

/**
 * Build the per-company columns for the /compare table from a slug list, in ONE
 * query: the company row plus its funding rounds (with round-level investors),
 * company-level investors, and competitors embedded. Excluded / unknown slugs
 * are dropped; rows are returned in the SAME order as the input `slugs` so the
 * comparison columns match the URL order. Returns [] on missing env or error.
 *
 * `totalRaised` mirrors the detail-page tile: max(stated total_raised_usd, sum
 * of known round amounts). `latestRound*` reads the denormalized columns from
 * migration 0028 (kept fresh by refresh-latest-round).
 */
export async function getCompaniesForCompare(
  slugs: string[],
): Promise<CompareCompany[]> {
  const wanted = slugs.filter((s) => typeof s === "string" && s.length > 0);
  if (wanted.length === 0) return [];

  const supabase = supabaseOrNull("getCompaniesForCompare");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("companies")
    .select(
      "slug, name, website, industry_group, hq_city, hq_state, status, " +
        "year_incorporated, employee_count_min, employee_count_max, " +
        "total_raised_usd, funding_round_count, latest_round_type, " +
        "latest_round_amount, latest_round_date, exclusion_reason, " +
        "funding_rounds(round_type, amount_raised, funding_round_investors(investors(name))), " +
        "company_investors(investors(name)), " +
        // The `competitors` table has TWO FKs to `companies` (company_id and
        // competitor_company_id), so a bare `competitors(...)` embed is ambiguous
        // and PostgREST 400s the WHOLE query — which silently returned [] here,
        // leaving /compare empty and every /vs pair a 404. Hint the owning-company
        // FK (company_id) to select this company's own competitor list, mirroring
        // how getAlternatives hints `companies!competitor_company_id`.
        "competitors!company_id(competitor_name, rank)",
    )
    .in("slug", wanted);

  if (error) {
    console.error("[getCompaniesForCompare] query failed:", error.message);
    return [];
  }

  interface NameJoin {
    investors: { name: string | null } | { name: string | null }[] | null;
  }
  interface CompareRow {
    slug: string | null;
    name: string | null;
    website: string | null;
    industry_group: string | null;
    hq_city: string | null;
    hq_state: string | null;
    status: string | null;
    year_incorporated: number | null;
    employee_count_min: number | null;
    employee_count_max: number | null;
    total_raised_usd: number | null;
    funding_round_count: number | null;
    latest_round_type: string | null;
    latest_round_amount: number | null;
    latest_round_date: string | null;
    exclusion_reason?: string | null;
    funding_rounds:
      | {
          round_type: string | null;
          amount_raised: number | null;
          funding_round_investors: NameJoin[] | null;
        }[]
      | null;
    company_investors: NameJoin[] | null;
    competitors: { competitor_name: string | null; rank: number | null }[] | null;
  }

  const bySlug = new Map<string, CompareCompany>();
  for (const row of (data ?? []) as unknown as CompareRow[]) {
    if (!row.slug || !row.name || row.exclusion_reason) continue;

    const rounds = row.funding_rounds ?? [];
    const totalRaised = computeTotalRaised(row.total_raised_usd, rounds).total;

    // Distinct investor names from BOTH the company-level link and round-level
    // links (a NameJoin's investors may be object or single-element array).
    const investorNames = new Set<string>();
    const addName = (j: NameJoin) => {
      const inv = Array.isArray(j.investors) ? j.investors[0] : j.investors;
      if (inv?.name) investorNames.add(inv.name);
    };
    for (const ci of row.company_investors ?? []) addName(ci);
    for (const r of rounds) {
      for (const fri of r.funding_round_investors ?? []) addName(fri);
    }

    const competitors = (row.competitors ?? [])
      .filter((c): c is { competitor_name: string; rank: number | null } =>
        Boolean(c.competitor_name),
      )
      .sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999))
      .map((c) => c.competitor_name)
      .slice(0, COMPARE_MAX_COMPETITORS);

    bySlug.set(row.slug, {
      slug: row.slug,
      name: row.name,
      website: row.website ?? null,
      industryGroup: row.industry_group ?? null,
      hqCity: row.hq_city ?? null,
      hqState: row.hq_state ?? null,
      status: row.status ?? "active",
      yearIncorporated: row.year_incorporated ?? null,
      employeeCountMin: row.employee_count_min ?? null,
      employeeCountMax: row.employee_count_max ?? null,
      totalRaised: totalRaised > 0 ? totalRaised : null,
      roundCount: row.funding_round_count ?? rounds.length,
      latestRoundType: row.latest_round_type ?? null,
      latestRoundAmount:
        row.latest_round_amount != null ? Number(row.latest_round_amount) : null,
      latestRoundDate: row.latest_round_date ?? null,
      investors: [...investorNames]
        .sort((a, b) => a.localeCompare(b, "en-US", { sensitivity: "base" }))
        .slice(0, COMPARE_MAX_INVESTORS),
      competitors,
    });
  }

  // Preserve the caller's slug order; drop unresolved/excluded.
  return wanted.flatMap((slug) => {
    const c = bySlug.get(slug);
    return c ? [c] : [];
  });
}

/**
 * Whether a RESOLVED competitor edge links the two companies in EITHER
 * direction — i.e. one lists the other as a competitor and that competitor was
 * matched to the other's company row (`competitor_company_id`, not just a
 * free-text name). This is the SEO-quality gate for /vs/[a]/[b]: only an edge
 * pair (with real funding on ≥1 side, checked page-side) is worth indexing;
 * arbitrary pairs render but stay `noindex`. Two lookups — slugs→ids, then a
 * single edge probe — both cheap and ISR-cached. Returns false on missing env,
 * error, or either slug being absent/excluded-away (an unmatched id).
 */
export async function areCompetitorsBySlug(
  slugA: string,
  slugB: string,
): Promise<boolean> {
  if (slugA === slugB) return false;
  const supabase = supabaseOrNull("areCompetitorsBySlug");
  if (!supabase) return false;

  const { data: idRows, error: idError } = await supabase
    .from("companies")
    .select("id, slug")
    .in("slug", [slugA, slugB]);

  if (idError || !idRows || idRows.length < 2) {
    if (idError) {
      console.error("[areCompetitorsBySlug] id lookup failed:", idError.message);
    }
    return false;
  }

  const idBySlug = new Map(
    (idRows as { id: string; slug: string }[]).map((r) => [r.slug, r.id]),
  );
  const idA = idBySlug.get(slugA);
  const idB = idBySlug.get(slugB);
  if (!idA || !idB) return false;

  const { data, error } = await supabase
    .from("competitors")
    .select("company_id")
    .or(
      `and(company_id.eq.${idA},competitor_company_id.eq.${idB}),and(company_id.eq.${idB},competitor_company_id.eq.${idA})`,
    )
    .limit(1);

  if (error) {
    console.error("[areCompetitorsBySlug] edge probe failed:", error.message);
    return false;
  }
  return (data ?? []).length > 0;
}

// ─── Co-investor signal (Task C5) ─────────────────────────────────────────────

// Cap on the "frequently co-invests with" firms surfaced on an investor page.
const CO_INVESTOR_LIMIT = 8;

/**
 * "Frequently co-invests with" for /investor/[slug]: other investors that
 * appear on the SAME funding rounds as this investor, ranked by the number of
 * shared rounds. Derived read-time from `funding_round_investors` (no stored
 * edge — a mega-fund would make this O(N^2) to persist):
 *
 *   1. The funding_round_ids this investor participated in.
 *   2. All (round, investor) links on those rounds; tally co-investors by the
 *      count of distinct shared rounds (excluding this investor itself).
 *   3. Resolve the top co-investor ids to slug + name.
 *
 * Returns [] on missing env, any error, or when this investor shares no round.
 */
export async function getCoInvestors(slug: string): Promise<CoInvestor[]> {
  const supabase = supabaseOrNull("getCoInvestors");
  if (!supabase) return [];

  // Resolve the investor slug → id.
  const { data: investor, error: investorError } = await supabase
    .from("investors")
    .select("id")
    .eq("slug", slug)
    .single();

  if (investorError || !investor) {
    if (investorError?.code !== "PGRST116") {
      console.error(
        "[getCoInvestors] investor lookup failed:",
        investorError?.message,
      );
    }
    return [];
  }

  const investorId = investor.id as string;

  // Step 1: rounds this investor is on. Order by funding_round_id so a
  // transient 1000-row cap truncates deterministically.
  const { data: ownLinks, error: ownError } = await supabase
    .from("funding_round_investors")
    .select("funding_round_id")
    .eq("investor_id", investorId)
    .order("funding_round_id", { ascending: true });

  if (ownError) {
    console.error("[getCoInvestors] own rounds query failed:", ownError.message);
    return [];
  }

  const roundIds = [
    ...new Set(
      ((ownLinks ?? []) as { funding_round_id: string | null }[])
        .map((r) => r.funding_round_id)
        .filter((id): id is string => id != null),
    ),
  ];
  if (roundIds.length === 0) return [];

  // Step 2: every investor link on those rounds; tally shared rounds per other
  // investor. A (co-investor, round) pair is deduped via a per-investor set so a
  // double-linked row can't inflate the count.
  const { data: coLinks, error: coError } = await supabase
    .from("funding_round_investors")
    .select("funding_round_id, investor_id")
    .in("funding_round_id", roundIds)
    .order("investor_id", { ascending: true });

  if (coError) {
    console.error("[getCoInvestors] co-investor query failed:", coError.message);
    return [];
  }

  const roundsByInvestor = new Map<string, Set<string>>();
  for (const r of (coLinks ?? []) as {
    funding_round_id: string | null;
    investor_id: string | null;
  }[]) {
    if (!r.investor_id || !r.funding_round_id) continue;
    if (r.investor_id === investorId) continue; // skip self
    let set = roundsByInvestor.get(r.investor_id);
    if (!set) {
      set = new Set<string>();
      roundsByInvestor.set(r.investor_id, set);
    }
    set.add(r.funding_round_id);
  }

  if (roundsByInvestor.size === 0) return [];

  const ranked = [...roundsByInvestor.entries()]
    .map(([id, set]) => ({ id, sharedRounds: set.size }))
    .sort((a, b) => b.sharedRounds - a.sharedRounds || a.id.localeCompare(b.id))
    .slice(0, CO_INVESTOR_LIMIT);

  // Step 3: resolve the top co-investor ids to slug + name.
  const { data: firms, error: firmsError } = await supabase
    .from("investors")
    .select("id, slug, name")
    .in(
      "id",
      ranked.map((r) => r.id),
    );

  if (firmsError) {
    console.error("[getCoInvestors] firm resolve failed:", firmsError.message);
    return [];
  }

  const firmById = new Map<string, { slug: string; name: string }>();
  for (const f of (firms ?? []) as {
    id: string | null;
    slug: string | null;
    name: string | null;
  }[]) {
    if (f.id && f.slug && f.name) firmById.set(f.id, { slug: f.slug, name: f.name });
  }

  return ranked.flatMap((r) => {
    const f = firmById.get(r.id);
    return f ? [{ slug: f.slug, name: f.name, sharedRounds: r.sharedRounds }] : [];
  });
}

// How many portfolio companies to pull per link path before aggregating — a
// bound so a mega-fund's thousands of links stay cheap. The momentum aggregate
// over the cap is representative; flagged rather than silently unbounded.
const PORTFOLIO_MOMENTUM_FETCH_CAP = 2000;
const TOP_HEATING_UP = 5;

// Nested company shape from the two portfolio link-path embeds.
interface NestedMomentumCompany {
  slug: string | null;
  name: string | null;
  momentum_score: number | null;
  momentum_why: string[] | null;
  exclusion_reason?: string | null;
}

/**
 * Portfolio momentum for /investor/[slug]: aggregate the pipeline
 * `momentum_score` (#181) across this investor's DISTINCT shown portfolio
 * companies — unioned over BOTH link paths (`company_investors` +
 * `funding_round_investors` → `funding_rounds` → `companies`) and deduped by
 * slug — and surface how many are "heating up" plus the hottest few. $0,
 * read-time, no new data; turns the flat portfolio list into a signal.
 *
 * Migration-order-free: `momentum_score` is migration 0039 (already on prod),
 * but a query error still degrades to null → the section hides. Returns an
 * object (with `scoredCount` possibly 0) on success; null on missing env / a
 * hard failure. Both embeds are unambiguous (each join table has ONE FK to its
 * target), so no FK hint is needed.
 */
export async function getInvestorPortfolioMomentum(
  slug: string,
): Promise<InvestorPortfolioMomentum | null> {
  const supabase = supabaseOrNull("getInvestorPortfolioMomentum");
  if (!supabase) return null;

  const { data: investor, error: investorError } = await supabase
    .from("investors")
    .select("id")
    .eq("slug", slug)
    .single();
  if (investorError || !investor) {
    if (investorError?.code !== "PGRST116") {
      console.error(
        "[getInvestorPortfolioMomentum] investor lookup failed:",
        investorError?.message,
      );
    }
    return null;
  }
  const investorId = investor.id as string;

  const cols = "slug, name, momentum_score, momentum_why, exclusion_reason";
  const [direct, viaRounds] = await Promise.all([
    supabase
      .from("company_investors")
      .select(`companies(${cols})`)
      .eq("investor_id", investorId)
      .limit(PORTFOLIO_MOMENTUM_FETCH_CAP),
    supabase
      .from("funding_round_investors")
      .select(`funding_rounds(companies(${cols}))`)
      .eq("investor_id", investorId)
      .limit(PORTFOLIO_MOMENTUM_FETCH_CAP),
  ]);
  // Degrade only when BOTH paths fail; a single failure still yields a partial
  // (honest, smaller) aggregate rather than hiding the section entirely.
  if (direct.error && viaRounds.error) {
    console.error(
      "[getInvestorPortfolioMomentum] portfolio queries failed:",
      direct.error.message,
    );
    return null;
  }

  // Union distinct SHOWN, SCORED companies by slug (first occurrence wins — the
  // same company via both paths carries the same score).
  const bySlug = new Map<string, PortfolioMomentumCompany>();
  const consider = (c: NestedMomentumCompany | null): void => {
    if (!c?.slug || !c.name || c.exclusion_reason) return;
    if (c.momentum_score == null || bySlug.has(c.slug)) return;
    bySlug.set(c.slug, {
      slug: c.slug,
      name: c.name,
      momentumScore: Number(c.momentum_score),
      momentumWhy: Array.isArray(c.momentum_why) ? c.momentum_why : [],
    });
  };
  for (const row of (direct.data ?? []) as {
    companies: NestedMomentumCompany | NestedMomentumCompany[] | null;
  }[]) {
    consider(Array.isArray(row.companies) ? row.companies[0] : row.companies);
  }
  for (const row of (viaRounds.data ?? []) as {
    funding_rounds:
      | { companies: NestedMomentumCompany | NestedMomentumCompany[] | null }
      | { companies: NestedMomentumCompany | NestedMomentumCompany[] | null }[]
      | null;
  }[]) {
    const fr = Array.isArray(row.funding_rounds)
      ? row.funding_rounds[0]
      : row.funding_rounds;
    if (!fr) continue;
    consider(Array.isArray(fr.companies) ? fr.companies[0] : fr.companies);
  }

  const scored = [...bySlug.values()];
  const scoredCount = scored.length;
  const heatingUp = scored
    .filter((c) => c.momentumScore >= MOMENTUM_BADGE_THRESHOLD)
    .sort(
      (a, b) => b.momentumScore - a.momentumScore || a.name.localeCompare(b.name),
    );
  const meanMomentum =
    scoredCount > 0
      ? scored.reduce((sum, c) => sum + c.momentumScore, 0) / scoredCount
      : null;

  return {
    scoredCount,
    heatingUpCount: heatingUp.length,
    meanMomentum,
    topHeatingUp: heatingUp.slice(0, TOP_HEATING_UP),
  };
}

// ─── Themes (Wave 3 E-3) ───────────────────────────────────────────────────────

// Nested shape from the company_themes → companies embed. Same object-or-
// single-element-array ambiguity PostgREST gives every embed; narrowed here.
interface NestedThemeMemberCompany {
  slug: string | null;
  name: string | null;
  hq_city: string | null;
  hq_state: string | null;
  industry_group: string | null;
  description_short: string | null;
  status: string | null;
  logo_url: string | null;
  created_at: string | null;
  exclusion_reason?: string | null;
}

type CompanyThemeJoin = {
  similarity: number | null;
  companies: NestedThemeMemberCompany | NestedThemeMemberCompany[] | null;
};

// Raw `themes` row shape shared by the index + detail queries.
interface ThemeRowRaw {
  slug: string | null;
  name: string | null;
  industry_group: string | null;
  description: string | null;
  company_count: number | null;
  funding_recent_usd: number | string | null;
  funding_prior_usd: number | string | null;
  funding_growth: number | string | null;
  updated_at: string | null;
}

const THEME_COLUMNS =
  "slug, name, industry_group, description, company_count, " +
  "funding_recent_usd, funding_prior_usd, funding_growth, updated_at";

function toThemeListRow(row: ThemeRowRaw): ThemeListRow | null {
  if (!row.slug || !row.name || !row.industry_group) return null;
  return {
    slug: row.slug,
    name: row.name,
    industry_group: row.industry_group,
    description: row.description ?? null,
    company_count: row.company_count ?? 0,
    funding_recent_usd: Number(row.funding_recent_usd ?? 0),
    funding_prior_usd: Number(row.funding_prior_usd ?? 0),
    funding_growth:
      row.funding_growth != null ? Number(row.funding_growth) : null,
    updated_at: row.updated_at ?? "",
  };
}

/**
 * Every theme, for the /themes index — ranked by trailing-2-quarter funding
 * growth ("what's heating up"), NULLS LAST so themes with an undefined
 * growth rate (zero prior-window funding) sort below any measured rate;
 * ties break on recent funding, then name. The theme count is small (a few
 * per industry) so no pagination. Returns [] on missing env, like every
 * other helper here.
 */
export async function listThemes(): Promise<ThemeListRow[]> {
  const supabase = supabaseOrNull("listThemes");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("themes")
    .select(THEME_COLUMNS)
    .order("funding_growth", { ascending: false, nullsFirst: false })
    .order("funding_recent_usd", { ascending: false })
    .order("name", { ascending: true });

  if (error) {
    console.error("[listThemes] query failed:", error.message);
    return [];
  }

  return ((data ?? []) as unknown as ThemeRowRaw[]).flatMap((row) => {
    const theme = toThemeListRow(row);
    return theme ? [theme] : [];
  });
}

/**
 * Themes whose `industry_group` matches, ranked exactly like {@link listThemes}
 * — the "sub-themes" module on /industry/[group]. These embedding-cluster
 * sub-groups (plus the funding chart) are the ONLY net-new content an industry
 * page carries over /companies?industry=X, so the page hard-guards on this
 * being non-empty for indexing (see the route). Returns [] on missing env.
 */
export async function listThemesByIndustry(
  industryGroup: string,
): Promise<ThemeListRow[]> {
  const supabase = supabaseOrNull("listThemesByIndustry");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("themes")
    .select(THEME_COLUMNS)
    .eq("industry_group", industryGroup)
    .order("funding_growth", { ascending: false, nullsFirst: false })
    .order("funding_recent_usd", { ascending: false })
    .order("name", { ascending: true });

  if (error) {
    console.error("[listThemesByIndustry] query failed:", error.message);
    return [];
  }

  return ((data ?? []) as unknown as ThemeRowRaw[]).flatMap((row) => {
    const theme = toThemeListRow(row);
    return theme ? [theme] : [];
  });
}

/**
 * Everything /themes/[slug] renders: the theme row, its member companies
 * (CompanyCard projection + similarity, ordered most-similar-first), and the
 * members' funding rounds (the page derives the by-quarter chart from these
 * stored, sourced rows — no new numbers are fabricated here).
 *
 * Excluded companies are dropped from the members (their /c/[slug] pages
 * 404) AND from the round aggregation, so they never surface on the page in
 * any form. Returns null for an unknown slug (→ 404) or missing env.
 */
export async function getThemeBySlug(slug: string): Promise<ThemeDetail | null> {
  const supabase = supabaseOrNull("getThemeBySlug");
  if (!supabase) return null;

  const { data: themeRow, error: themeError } = await supabase
    .from("themes")
    .select(`id, ${THEME_COLUMNS}`)
    .eq("slug", slug)
    .single();

  if (themeError || !themeRow) {
    if (themeError?.code !== "PGRST116") {
      console.error("[getThemeBySlug] theme query failed:", themeError?.message);
    }
    return null;
  }
  const theme = toThemeListRow(themeRow as unknown as ThemeRowRaw);
  if (!theme) return null;
  const themeId = (themeRow as unknown as { id: string | null }).id;

  const { data: memberRows, error: membersError } = await supabase
    .from("company_themes")
    .select(
      "similarity, companies(slug, name, hq_city, hq_state, industry_group, description_short, status, logo_url, created_at, exclusion_reason)",
    )
    .eq("theme_id", themeId)
    .order("similarity", { ascending: false });

  if (membersError) {
    console.error(
      "[getThemeBySlug] members query failed:",
      membersError.message,
    );
    // The theme header is still a valid (if thin) page.
    return { theme, members: [], rounds: [] };
  }

  const members: ThemeMember[] = [];
  for (const row of (memberRows ?? []) as unknown as CompanyThemeJoin[]) {
    const c = Array.isArray(row.companies) ? row.companies[0] : row.companies;
    // Drop unresolved joins AND excluded companies — the latter 404 on
    // /c/[slug] and must never surface here in any form.
    if (!c?.slug || !c.name || c.exclusion_reason) continue;
    members.push({
      slug: c.slug,
      name: c.name,
      hq_city: c.hq_city ?? null,
      hq_state: c.hq_state ?? null,
      industry_group: c.industry_group ?? null,
      description_short: c.description_short ?? null,
      status: c.status ?? "active",
      logo_url: c.logo_url ?? null,
      similarity: row.similarity != null ? Number(row.similarity) : 0,
      created_at: c.created_at ?? "",
    });
  }

  // The members' funding rounds, for the by-quarter chart. Member ids are
  // not part of the card projection, so resolve rounds via the slugs we
  // already hold — one .in() over an indexed FK join stays cheap at theme
  // sizes (≲ a few hundred members).
  let rounds: ThemeRound[] = [];
  if (members.length > 0) {
    const { data: roundRows, error: roundsError } = await supabase
      .from("funding_rounds")
      .select("announced_date, amount_raised, companies!inner(slug)")
      .in(
        "companies.slug",
        members.map((m) => m.slug),
      );
    if (roundsError) {
      console.error(
        "[getThemeBySlug] rounds query failed:",
        roundsError.message,
      );
    } else {
      rounds = ((roundRows ?? []) as {
        announced_date: string | null;
        amount_raised: number | string | null;
      }[]).map((r) => ({
        announced_date: r.announced_date ?? null,
        amount_raised: r.amount_raised != null ? Number(r.amount_raised) : null,
      }));
    }
  }

  return { theme, members, rounds };
}

/**
 * Minimum members a theme needs before /themes/<slug> earns a sitemap entry.
 * Mirrors {@link MIN_ALTERNATIVES_COMPETITOR_COUNT} / MIN_TAG_COMPANY_COUNT:
 * thin pages stay reachable (the index links every theme) but out of the
 * sitemap. Reads the build-time `company_count` the pipeline stamped — a
 * member excluded after the build may briefly overcount, exactly like a
 * competitor row whose company was excluded still counts for alternatives.
 */
const MIN_THEME_MEMBER_COUNT = 3;

/** Slugs (+ updated_at) of themes with ≥ {@link MIN_THEME_MEMBER_COUNT}
 * members, for the sitemap. Returns [] on missing env or error so the
 * sitemap still builds with its other entries. */
export async function listAllThemeSlugs(): Promise<
  { slug: string; updated_at: string | null }[]
> {
  const supabase = supabaseOrNull("listAllThemeSlugs");
  if (!supabase) return [];

  const { data, error } = await supabase
    .from("themes")
    .select("slug, updated_at")
    .gte("company_count", MIN_THEME_MEMBER_COUNT)
    .order("slug", { ascending: true });

  if (error) {
    console.error("[listAllThemeSlugs] query failed:", error.message);
    return [];
  }

  return ((data ?? []) as { slug: string | null; updated_at: string | null }[])
    .flatMap((row) => (row.slug ? [{ slug: row.slug, updated_at: row.updated_at }] : []));
}
