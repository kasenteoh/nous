// Server-side query helpers. This file must never be imported from a client
// component — it uses createSupabaseServerClient() which requires the service
// role key to be present in the server environment.

import { createSupabaseServerClient } from "@/lib/db";
import type {
  CompanyDetail,
  CompanyInvestorRow,
  CompanyListRow,
  CompanyRow,
  CompetitorRow,
  CompetitorWithResolved,
  FundingRound,
  FundingRoundWithInvestors,
  InvestorDetail,
  InvestorListRow,
  InvestorRoundRow,
  InvestorSlugRow,
  NewsArticleRow,
  PersonRow,
} from "@/lib/types";

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

/** Sort options exposed by the index page. */
export type CompanyListSort = "name_asc" | "name_desc" | "recent";

/** Filters + paging accepted by {@link listCompanies}. */
export interface CompanyListOptions {
  search?: string;
  industry_group?: string;
  discovered_via?: string;
  /** Filter to companies whose `tags` array contains this exact value. */
  tag?: string;
  /** Filter to companies whose `hq_state` exactly matches this value. */
  state?: string;
  sort?: CompanyListSort;
  limit?: number;
  offset?: number;
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
function sanitizeIlikeTerm(term: string): string {
  return term.replace(/[,()%*\\]/g, " ").replace(/\s+/g, " ").trim();
}

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

  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    // Missing env vars — expected during build-time prerender or local dev without .env.local.
    console.warn("[listCompanies] Supabase not configured:", (err as Error).message);
    return { rows: [], total: 0 };
  }

  // `count: "exact"` makes PostgREST return the total matching the filters
  // (ignoring `.range()`), so we get rows + total in a single round trip.
  let query = supabase
    .from("companies")
    .select(
      "slug, name, hq_city, hq_state, industry_group, description_short, status",
      { count: "exact" },
    );

  const search = opts.search ? sanitizeIlikeTerm(opts.search) : "";
  if (search) {
    query = query.or(
      `name.ilike.%${search}%,description_short.ilike.%${search}%`,
    );
  }
  if (opts.industry_group) {
    query = query.eq("industry_group", opts.industry_group);
  }
  if (opts.discovered_via) {
    query = query.eq("discovered_via", opts.discovered_via);
  }
  if (opts.tag) {
    // `contains` checks that the text[] column includes the exact element.
    // Never use ilike here — a substring match would conflate e.g. "ai" with "ai-infrastructure".
    query = query.contains("tags", [opts.tag]);
  }
  if (opts.state) {
    query = query.eq("hq_state", opts.state);
  }

  switch (opts.sort) {
    case "name_desc":
      query = query.order("name", { ascending: false });
      break;
    case "recent":
      query = query.order("created_at", { ascending: false });
      break;
    default:
      query = query.order("name", { ascending: true });
  }

  const { data: companies, error, count } = await query.range(
    offset,
    offset + limit - 1,
  );

  if (error) {
    console.error("[listCompanies] companies query failed:", error.message);
    return { rows: [], total: 0 };
  }

  const rows = (companies ?? []).map((c) => ({
    slug: c.slug as string,
    name: c.name as string,
    hq_city: (c.hq_city as string | null) ?? null,
    hq_state: (c.hq_state as string | null) ?? null,
    industry_group: (c.industry_group as string | null) ?? null,
    description_short: (c.description_short as string | null) ?? null,
    status: c.status as string,
  }));

  return { rows, total: count ?? rows.length };
}

/**
 * Distinct, non-null `industry_group` values for the index filter dropdown,
 * deduped in-process from a full keyset scan via {@link scanCompanies}. A flat
 * select is silently capped at 1000 rows by PostgREST (`.limit(5000)` does not
 * override the server cap), which dropped every group that only occurs outside
 * that arbitrary unordered sample. `discovered_via` is a small fixed enum, so
 * the page hardcodes those options rather than querying for them.
 */
export async function listIndustryGroups(): Promise<string[]> {
  const rows = await scanCompanies(
    "listIndustryGroups",
    "slug, industry_group",
    "industry_group",
  );
  if (rows === null) return [];

  const seen = new Set<string>();
  for (const row of rows) {
    const value = row.industry_group as string | null;
    if (value) seen.add(value);
  }
  return [...seen].sort((a, b) => a.localeCompare(b));
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

/**
 * The latest funding rounds with a known announce date, newest first, joined
 * with the company's name and slug. Rows whose company join is missing are
 * dropped (every fact on the page must link somewhere).
 */
export async function listRecentFundings(
  limit = 5,
): Promise<RecentFundingRow[]> {
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[listRecentFundings] Supabase not configured:",
      (err as Error).message,
    );
    return [];
  }

  const { data, error } = await supabase
    .from("funding_rounds")
    .select("round_type, amount_raised, announced_date, companies(name, slug)")
    .not("announced_date", "is", null)
    .order("announced_date", { ascending: false })
    .limit(limit);

  if (error) {
    console.error("[listRecentFundings] query failed:", error.message);
    return [];
  }

  type Row = {
    round_type: string | null;
    amount_raised: number | null;
    announced_date: string;
    companies: NestedFundingCompany | NestedFundingCompany[] | null;
  };

  return ((data ?? []) as Row[]).flatMap((row) => {
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
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[listNewestCompanies] Supabase not configured:",
      (err as Error).message,
    );
    return [];
  }

  // Over-fetch so described companies can be preferred without a second query.
  const { data, error } = await supabase
    .from("companies")
    .select("slug, name, description_short")
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

/** Exact number of companies in the index (head-only count). */
export async function countCompanies(): Promise<number> {
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[countCompanies] Supabase not configured:",
      (err as Error).message,
    );
    return 0;
  }

  const { count, error } = await supabase
    .from("companies")
    .select("id", { count: "exact", head: true });

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
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[getRandomCompanySlug] Supabase not configured:",
      (err as Error).message,
    );
    return null;
  }

  const { count, error: countError } = await supabase
    .from("companies")
    .select("id", { count: "exact", head: true });

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
): Promise<TableScanResult> {
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(`[${label}] Supabase not configured:`, (err as Error).message);
    return { rows: [], ok: false };
  }

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
): Promise<Record<string, unknown>[] | null> {
  const { rows, ok } = await scanTable("companies", label, select, notNullColumn);
  return ok ? rows : null;
}

/**
 * Every company slug + updated_at, for app/sitemap.ts. Keyset-paginated via
 * {@link scanCompanies} — see its doc for why a flat select would truncate.
 * Returns [] on error or missing env — CI builds without Supabase secrets and
 * the sitemap must still build with just its static entries.
 */
export async function listAllCompanySlugs(): Promise<CompanySlugRow[]> {
  const rows = await scanCompanies("listAllCompanySlugs", "slug, updated_at");
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
 * with its rounds' amounts embedded (`funding_rounds(amount_raised)`);
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
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[getCompanyOgData] Supabase not configured:",
      (err as Error).message,
    );
    return null;
  }

  const { data: company, error: companyError } = await supabase
    .from("companies")
    .select(
      "name, industry_group, total_raised_usd, funding_rounds(amount_raised)",
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

  // Runtime-built select string → supabase-js can't parse the columns, so
  // narrow through a local row shape (same idiom as scanTable).
  const row = company as unknown as {
    name: string;
    industry_group: string | null;
    total_raised_usd: number | null;
    funding_rounds:
      | { amount_raised: number | null }[]
      | { amount_raised: number | null }
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

  const computedTotal = rounds.reduce<number>((acc, r) => {
    return r.amount_raised != null ? acc + Number(r.amount_raised) : acc;
  }, 0);
  const statedTotal =
    row.total_raised_usd != null ? Number(row.total_raised_usd) : 0;

  return {
    name: row.name,
    industry_group: row.industry_group ?? null,
    totalRaised: Math.max(statedTotal, computedTotal),
  };
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
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    // Missing env vars — expected during build-time prerender or local dev without .env.local.
    console.warn("[getCompanyBySlug] Supabase not configured:", (err as Error).message);
    return null;
  }

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

  const companyId = company.id as string;

  // 2, 3 & 4: fetch people, funding rounds (with nested investor joins), and
  // competitors (with resolved company) in parallel.
  const [peopleResult, roundsResult, competitorsResult, investorsResult, newsResult] =
    await Promise.all([
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
        .select("*, competitor_company:companies!competitor_company_id(slug, name)")
        .eq("company_id", companyId)
        .order("rank", { ascending: true }),

      supabase
        .from("company_investors")
        .select("is_lead, source, investors(name, website)")
        .eq("company_id", companyId),

      supabase
        .from("news_articles")
        .select("id, url, title, source, published_date")
        .eq("company_id", companyId)
        .order("published_date", { ascending: false, nullsFirst: false }),
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
      nested && nested.slug && nested.name
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

  return {
    company: company as unknown as CompanyRow,
    people,
    fundingRounds,
    competitors,
    investors,
    news,
  };
}

// ─── Tag / location SEO queries ───────────────────────────────────────────────

/**
 * All distinct, non-null tag values across the companies table, sorted.
 * PostgREST has no native `unnest` (nor DISTINCT) and caps every response at
 * 1000 rows, so a flat select would silently sample ~1/4 of the ~4,200-row
 * catalog. Instead we keyset-scan the whole table via {@link scanCompanies},
 * then flatten + deduplicate the `tags` arrays in-process.
 */
export async function listAllTags(): Promise<string[]> {
  const rows = await scanCompanies("listAllTags", "slug, tags", "tags");
  if (rows === null) return [];

  const seen = new Set<string>();
  for (const row of rows) {
    const tags = row.tags as string[] | null;
    if (Array.isArray(tags)) {
      for (const t of tags) {
        if (t) seen.add(t);
      }
    }
  }
  return [...seen].sort((a, b) => a.localeCompare(b));
}

/**
 * All distinct, non-null `hq_state` values, sorted. Same full keyset scan +
 * in-process dedup idiom as {@link listAllTags} — PostgREST's 1000-row
 * response cap means anything short of paging the whole table drops rows.
 */
export async function listAllStates(): Promise<string[]> {
  const rows = await scanCompanies("listAllStates", "slug, hq_state", "hq_state");
  if (rows === null) return [];

  const seen = new Set<string>();
  for (const row of rows) {
    const value = row.hq_state as string | null;
    if (value) seen.add(value);
  }
  return [...seen].sort((a, b) => a.localeCompare(b));
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
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[listNewThisWeekCompanies] Supabase not configured:",
      (err as Error).message,
    );
    return [];
  }

  const cutoff = new Date(Date.now() - days * 86400e3).toISOString();

  const { data, error } = await supabase
    .from("companies")
    .select("slug, name, description_short, industry_group, created_at")
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
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[listNewThisWeekFundingRounds] Supabase not configured:",
      (err as Error).message,
    );
    return [];
  }

  const cutoff = new Date(Date.now() - days * 86400e3).toISOString();

  const { data, error } = await supabase
    .from("funding_rounds")
    .select(
      "round_type, amount_raised, announced_date, created_at, companies(slug, name)",
    )
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
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[countNewThisWeek] Supabase not configured:",
      (err as Error).message,
    );
    return { companies: 0, rounds: 0 };
  }

  const cutoff = new Date(Date.now() - days * 86400e3).toISOString();

  const [companiesResult, roundsResult] = await Promise.all([
    supabase
      .from("companies")
      .select("id", { count: "exact", head: true })
      .gte("created_at", cutoff),
    supabase
      .from("funding_rounds")
      .select("id", { count: "exact", head: true })
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
 * Ranking uses PostgREST's embedded-aggregate ordering: `company_investors(count)`
 * embeds the per-investor holding count, and `.order("count", { referencedTable })`
 * sorts by it server-side — no in-process sort over the whole table (which can
 * run to thousands of firms). `name` is the deterministic tiebreaker so paging is
 * stable when many firms share a count. `count: "exact"` returns the unfiltered
 * total in the same round trip.
 */
export async function listInvestors(
  opts: { limit?: number; offset?: number } = {},
): Promise<InvestorListResult> {
  const limit = opts.limit ?? 30;
  const offset = opts.offset ?? 0;

  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn("[listInvestors] Supabase not configured:", (err as Error).message);
    return { rows: [], total: 0 };
  }

  const { data, error, count } = await supabase
    .from("investors")
    .select("slug, name, type, company_investors(count)", { count: "exact" })
    .order("count", { referencedTable: "company_investors", ascending: false })
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
    // Embedded aggregate comes back as [{ count: number }] (or [] / null).
    company_investors: { count: number }[] | { count: number } | null;
  };

  const rows = ((data ?? []) as Row[]).flatMap((r) => {
    if (!r.slug || !r.name) return [];
    const agg = Array.isArray(r.company_investors)
      ? r.company_investors[0]
      : r.company_investors;
    return [
      {
        slug: r.slug,
        name: r.name,
        type: r.type ?? "unknown",
        portfolioCount: agg?.count ?? 0,
      },
    ];
  });

  return { rows, total: count ?? rows.length };
}

/**
 * Full detail for a single investor by slug, or null when the slug is unknown.
 *
 * Three queries:
 *   1. investors — the firm row (id, display fields).
 *   2. company_investors → companies — the portfolio, shaped for CompanyCard.
 *   3. funding_round_investors → funding_rounds → companies — rounds this firm
 *      led or participated in, flattened with the funded company.
 */
export async function getInvestorBySlug(
  slug: string,
): Promise<InvestorDetail | null> {
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[getInvestorBySlug] Supabase not configured:",
      (err as Error).message,
    );
    return null;
  }

  // 1. The investor row.
  const { data: investor, error: investorError } = await supabase
    .from("investors")
    .select("id, slug, name, type, description, website")
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
        "companies(slug, name, hq_city, hq_state, industry_group, description_short, status)",
      )
      .eq("investor_id", investorId),

    supabase
      .from("funding_round_investors")
      .select(
        "is_lead, funding_rounds(id, round_type, amount_raised, announced_date, primary_news_url, companies(slug, name))",
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
  type PortfolioJoin = {
    companies:
      | {
          slug: string | null;
          name: string | null;
          hq_city: string | null;
          hq_state: string | null;
          industry_group: string | null;
          description_short: string | null;
          status: string | null;
        }
      | {
          slug: string | null;
          name: string | null;
          hq_city: string | null;
          hq_state: string | null;
          industry_group: string | null;
          description_short: string | null;
          status: string | null;
        }[]
      | null;
  };

  const portfolio: CompanyListRow[] = ((portfolioResult.data ?? []) as PortfolioJoin[])
    .flatMap((row) => {
      const c = Array.isArray(row.companies) ? row.companies[0] : row.companies;
      if (!c?.slug || !c.name) return [];
      return [
        {
          slug: c.slug,
          name: c.name,
          hq_city: c.hq_city ?? null,
          hq_state: c.hq_state ?? null,
          industry_group: c.industry_group ?? null,
          description_short: c.description_short ?? null,
          status: c.status ?? "active",
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
            | { slug: string | null; name: string | null }
            | { slug: string | null; name: string | null }[]
            | null;
        }
      | {
          id: string;
          round_type: string | null;
          amount_raised: number | null;
          announced_date: string | null;
          primary_news_url: string | null;
          companies:
            | { slug: string | null; name: string | null }
            | { slug: string | null; name: string | null }[]
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
      if (!c?.slug || !c.name) return [];
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

  return {
    slug: investor.slug as string,
    name: investor.name as string,
    type: (investor.type as string | null) ?? "unknown",
    description: (investor.description as string | null) ?? null,
    website: (investor.website as string | null) ?? null,
    portfolio,
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
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn("[countInvestors] Supabase not configured:", (err as Error).message);
    return 0;
  }

  const { count, error } = await supabase
    .from("investors")
    .select("id", { count: "exact", head: true });

  if (error) {
    console.error("[countInvestors] query failed:", error.message);
    return 0;
  }
  return count ?? 0;
}
