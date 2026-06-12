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
 * Distinct, non-null `industry_group` values for the index filter dropdown.
 * Deduped client-side; the catalog is small enough that this is cheaper than a
 * dedicated RPC. `discovered_via` is a small fixed enum, so the page hardcodes
 * those options rather than querying for them.
 */
export async function listIndustryGroups(): Promise<string[]> {
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn("[listIndustryGroups] Supabase not configured:", (err as Error).message);
    return [];
  }

  const { data, error } = await supabase
    .from("companies")
    .select("industry_group")
    .not("industry_group", "is", null)
    .limit(5000);

  if (error) {
    console.error("[listIndustryGroups] query failed:", error.message);
    return [];
  }

  const seen = new Set<string>();
  for (const row of data ?? []) {
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
 * Count industry_group frequencies in-process (same column fetch as
 * listIndustryGroups — the catalog is small) and return the top N.
 */
export async function getIndustrySummary(topN = 6): Promise<IndustrySummary> {
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[getIndustrySummary] Supabase not configured:",
      (err as Error).message,
    );
    return { top: [], moreCount: 0 };
  }

  const { data, error } = await supabase
    .from("companies")
    .select("industry_group")
    .not("industry_group", "is", null)
    .limit(5000);

  if (error) {
    console.error("[getIndustrySummary] query failed:", error.message);
    return { top: [], moreCount: 0 };
  }

  const counts = new Map<string, number>();
  for (const row of data ?? []) {
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

/**
 * Keyset-paginated full scan of the `companies` table, shared by the sitemap
 * queries below. PostgREST caps every response at 1000 rows regardless of
 * `.limit()`, and the catalog holds ~4,200 companies, so any single-shot
 * select silently truncates. This walks the table ordered by `slug` (unique,
 * so the cursor strictly advances) in 1000-row pages via `.gt("slug", cursor)`
 * until a short page. Keyset beats offset `.range()` here: termination is
 * provable, and rows inserted mid-iteration can't shift offsets and cause
 * skips or duplicates. A hard `maxPages` bound caps the walk at 50k rows —
 * also Google's per-sitemap URL cap — so a pathological loop can never hang
 * the build; hitting the bound warns loudly instead of truncating silently.
 *
 * `select` must include `slug` (the cursor column). When `notNullColumn` is
 * given, rows where that column is null are filtered out server-side. Returns
 * null when Supabase is unconfigured or any page fails (both logged under
 * `label`) so callers can fall back to their empty value.
 */
async function scanCompanies(
  label: string,
  select: string,
  notNullColumn?: string,
): Promise<Record<string, unknown>[] | null> {
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(`[${label}] Supabase not configured:`, (err as Error).message);
    return null;
  }

  const pageSize = 1000;
  const maxPages = 50; // 50k rows — Google's per-sitemap URL cap.
  const all: Record<string, unknown>[] = [];
  let lastSlug: string | null = null;

  for (let page = 0; page < maxPages; page++) {
    let query = supabase
      .from("companies")
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
      return null;
    }

    // supabase-js types `.select()` results by parsing the literal column
    // string; with a runtime `string` it falls back to GenericStringError, so
    // widen through unknown — callers narrow per-column as elsewhere in this file.
    const rows = (data ?? []) as unknown as Record<string, unknown>[];
    all.push(...rows);

    // A short (or empty) page means we've drained the table.
    if (rows.length < pageSize) return all;

    lastSlug = rows[rows.length - 1].slug as string;
  }

  console.warn(
    `[${label}] hit maxPages=${maxPages} (${all.length} rows); ` +
      "results may be truncated — split into multiple sitemaps before raising the cap.",
  );
  return all;
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
  /** Sum of known round amounts in USD; 0 when nothing is known. */
  totalRaised: number;
}

/**
 * Lean fetch for app/c/[slug]/opengraph-image.tsx — deliberately not
 * getCompanyBySlug, which fans out into five queries the card doesn't need.
 * One query: the company row with its rounds' amounts embedded
 * (`funding_rounds(amount_raised)`), summed in-process.
 * Returns null when the slug is unknown (caller falls back to the site card).
 * Missing/empty rounds degrade to totalRaised = 0 — the card still renders,
 * just without the raised line.
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
    .select("name, industry_group, funding_rounds(amount_raised)")
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

  // One-to-many embed: PostgREST returns an array, but guard null/object
  // shapes so a missing join degrades to 0 instead of breaking the card.
  const roundsRaw = company.funding_rounds as
    | { amount_raised: number | null }[]
    | { amount_raised: number | null }
    | null;
  const rounds = Array.isArray(roundsRaw)
    ? roundsRaw
    : roundsRaw != null
      ? [roundsRaw]
      : [];

  const totalRaised = rounds.reduce<number>((acc, r) => {
    return r.amount_raised != null ? acc + Number(r.amount_raised) : acc;
  }, 0);

  return {
    name: company.name as string,
    industry_group: (company.industry_group as string | null) ?? null,
    totalRaised,
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
