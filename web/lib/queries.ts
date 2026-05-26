// Server-side query helpers. This file must never be imported from a client
// component — it uses createSupabaseServerClient() which requires the service
// role key to be present in the server environment.

import { createSupabaseServerClient } from "@/lib/db";
import type {
  CompanyDetail,
  CompanyListRow,
  CompanyRow,
  FilingRow,
  RelatedPersonRow,
} from "@/lib/types";

/**
 * Return a paginated list of companies with their latest filing snapshot.
 *
 * Strategy: two queries.
 *   1. Fetch companies (ordered by name).
 *   2. For each company, fetch its most-recent filing (filing_date desc, limit 1).
 *
 * This is intentionally simple for M1's 50-row pages. A Postgres view or RPC
 * would be cleaner at scale, but YAGNI until we have meaningful traffic.
 */
export async function listCompanies(opts: {
  limit?: number;
  offset?: number;
}): Promise<CompanyListRow[]> {
  const limit = opts.limit ?? 50;
  const offset = opts.offset ?? 0;

  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    // Missing env vars — expected during build-time prerender or local dev without .env.local.
    console.warn("[listCompanies] Supabase not configured:", (err as Error).message);
    return [];
  }

  const { data: companies, error } = await supabase
    .from("companies")
    .select(
      "slug, name, hq_city, hq_state, industry_group, description_short, id",
    )
    .order("name", { ascending: true })
    .range(offset, offset + limit - 1);

  if (error) {
    console.error("[listCompanies] companies query failed:", error.message);
    return [];
  }
  if (!companies || companies.length === 0) return [];

  // Fetch the latest filing for each company in parallel.
  const companyIds = companies.map((c) => c.id as string);

  const { data: latestFilings, error: filingError } = await supabase
    .from("filings")
    .select("company_id, filing_date, offering_amount_total")
    .in("company_id", companyIds)
    .order("filing_date", { ascending: false });

  if (filingError) {
    console.error(
      "[listCompanies] filings query failed:",
      filingError.message,
    );
  }

  // Build a map: company_id → latest filing row (first seen = most recent due to ordering).
  const latestByCompany = new Map<
    string,
    { filing_date: string; offering_amount_total: number | null }
  >();
  for (const f of latestFilings ?? []) {
    const id = f.company_id as string;
    if (!latestByCompany.has(id)) {
      latestByCompany.set(id, {
        filing_date: f.filing_date as string,
        offering_amount_total: f.offering_amount_total as number | null,
      });
    }
  }

  return companies.map((c) => {
    const latest = latestByCompany.get(c.id as string);
    return {
      slug: c.slug as string,
      name: c.name as string,
      hq_city: (c.hq_city as string | null) ?? null,
      hq_state: (c.hq_state as string | null) ?? null,
      industry_group: (c.industry_group as string | null) ?? null,
      description_short: (c.description_short as string | null) ?? null,
      latest_filing_date: latest?.filing_date ?? null,
      latest_offering_amount: latest?.offering_amount_total ?? null,
    };
  });
}

/**
 * Return the full detail for a single company identified by slug.
 * Returns null when the slug does not exist.
 *
 * Three queries:
 *   1. companies — the main row.
 *   2. filings — all filings for this company, newest first.
 *   3. related_persons — all people linked to this company, ordered so the most
 *      recent filing's people appear first (via filing_id ordering matching the
 *      filing date desc order).
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

  // 2 & 3: fetch filings and related persons in parallel.
  const [filingsResult, personsResult] = await Promise.all([
    supabase
      .from("filings")
      .select("*")
      .eq("company_id", companyId)
      .order("filing_date", { ascending: false }),

    supabase
      .from("related_persons")
      .select("*")
      .eq("company_id", companyId),
  ]);

  if (filingsResult.error) {
    console.error(
      "[getCompanyBySlug] filings query failed:",
      filingsResult.error.message,
    );
  }
  if (personsResult.error) {
    console.error(
      "[getCompanyBySlug] related_persons query failed:",
      personsResult.error.message,
    );
  }

  const filings = (filingsResult.data ?? []) as FilingRow[];
  const rawPersons = (personsResult.data ?? []) as RelatedPersonRow[];

  // Sort related persons: most recent filing's people first.
  // Build a map: filing_id → filing_date for ordering.
  const filingDateByFiling = new Map<string, string>(
    filings.map((f) => [f.id, f.filing_date]),
  );

  const relatedPersons = [...rawPersons].sort((a, b) => {
    const da = filingDateByFiling.get(a.filing_id) ?? "";
    const db2 = filingDateByFiling.get(b.filing_id) ?? "";
    return db2.localeCompare(da); // desc
  });

  return {
    company: company as unknown as CompanyRow,
    filings,
    relatedPersons,
  };
}
