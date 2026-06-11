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
      "slug, name, hq_city, hq_state, industry_group, description_short",
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
