"use server";

// Server action backing the (client-only) /watchlist page. The watchlist itself
// lives in the browser's localStorage; this action hydrates a slug list into
// CompanyListRow cards server-side so the Supabase service-role key never
// reaches the client. Lean projection — the same columns CompanyCard renders.

import { createSupabaseServerClient } from "@/lib/db";
import type { CompanyListRow } from "@/lib/types";

// Cap the number of slugs honored per call so a tampered localStorage payload
// can't ask us to build an unbounded `.in(...)` list.
const MAX_WATCHLIST = 200;

/**
 * Resolve a list of company slugs to display cards, dropping excluded companies
 * (their /c/[slug] page 404s) and any unknown slug. Returns rows in the same
 * order as the input `slugs` so the watchlist reflects the user's list order.
 */
export async function fetchWatchlistCompanies(
  slugs: string[],
): Promise<CompanyListRow[]> {
  const wanted = slugs
    .filter((s): s is string => typeof s === "string" && s.length > 0)
    .slice(0, MAX_WATCHLIST);
  if (wanted.length === 0) return [];

  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[fetchWatchlistCompanies] Supabase not configured:",
      (err as Error).message,
    );
    return [];
  }

  const { data, error } = await supabase
    .from("companies")
    .select(
      "slug, name, hq_city, hq_state, industry_group, description_short, status, exclusion_reason",
    )
    .in("slug", wanted);

  if (error) {
    console.error("[fetchWatchlistCompanies] query failed:", error.message);
    return [];
  }

  const bySlug = new Map<string, CompanyListRow>();
  for (const c of (data ?? []) as {
    slug: string | null;
    name: string | null;
    hq_city: string | null;
    hq_state: string | null;
    industry_group: string | null;
    description_short: string | null;
    status: string | null;
    exclusion_reason?: string | null;
  }[]) {
    if (!c.slug || !c.name || c.exclusion_reason) continue;
    bySlug.set(c.slug, {
      slug: c.slug,
      name: c.name,
      hq_city: c.hq_city ?? null,
      hq_state: c.hq_state ?? null,
      industry_group: c.industry_group ?? null,
      description_short: c.description_short ?? null,
      status: c.status ?? "active",
    });
  }

  // Preserve the caller's slug order; drop unresolved/excluded.
  return wanted.flatMap((slug) => {
    const row = bySlug.get(slug);
    return row ? [row] : [];
  });
}
