// CSV export of the current /companies filter (Task C4). Re-runs the exact
// same filter set SERVER-SIDE — reusing the shared filter helper + catalog bar
// from queries.ts so it can never drift from the browse page — keyset-scans
// EVERY match (no pagination cap), and streams text/csv. The Supabase
// service-role client lives only here on the server; nothing about it reaches
// the browser (the response is plain CSV).

import { createSupabaseServerClient } from "@/lib/db";
import { resolveIndustrySlug } from "@/lib/industry";
import {
  applyCompanyFilters,
  CATALOG_BAR_OR,
  listCanonicalIndustries,
  sanitizeIlikeTerm,
  type CompanyListOptions,
} from "@/lib/queries";

// Always run at request time — the export reflects the live filter, never a
// build-time snapshot.
export const dynamic = "force-dynamic";

// Keyset page size. PostgREST caps any response at 1000 rows regardless of
// .limit(), so we page by slug until a short page (same idiom as scanTable).
const PAGE_SIZE = 1000;
// Hard bound on the walk so a pathological loop can't hang the request; 50k
// rows is far above the catalog size.
const MAX_PAGES = 50;

const STAGE_OPTIONS = [
  "Pre-Seed",
  "Seed",
  "Series A",
  "Series B",
  "Series C",
  "Series D",
  "Series E",
];

/** Parse a query param as a non-negative number, or undefined. */
function num(v: string | null): number | undefined {
  if (!v) return undefined;
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : undefined;
}

/** Build CompanyListOptions from the request's search params (mirrors the
 *  /companies page parsing — the export carries the same querystring). */
function optionsFromParams(params: URLSearchParams): CompanyListOptions {
  const stageRaw = params.get("stage") ?? "";
  return {
    search: (params.get("q") ?? "").trim() || undefined,
    industry_group: params.get("industry") || undefined,
    discovered_via: params.get("source") || undefined,
    min_raised: num(params.get("min_raised")),
    max_raised: num(params.get("max_raised")),
    founded_after: num(params.get("founded_after")),
    founded_before: num(params.get("founded_before")),
    emp_min: num(params.get("emp_min")),
    emp_max: num(params.get("emp_max")),
    stage: STAGE_OPTIONS.includes(stageRaw) ? stageRaw : undefined,
    funded_since_days: num(params.get("funded_since_days")) || undefined,
  };
}

// ── CSV helpers ──────────────────────────────────────────────────────────────

/** RFC-4180-quote a single field: wrap in quotes and double any inner quote.
 *  Always quoting keeps embedded commas/newlines safe regardless of content. */
function csvField(value: string | number | null | undefined): string {
  if (value == null) return '""';
  return `"${String(value).replace(/"/g, '""')}"`;
}

const COLUMNS = [
  "name",
  "slug",
  "website",
  "industry",
  "hq_city",
  "hq_state",
  "latest_round_type",
  "latest_round_amount_usd",
  "latest_round_date",
  "total_raised_usd",
  "employees_min",
  "employees_max",
  "investors",
] as const;

// Nested shape from the company_investors → investors embed.
interface ExportInvestorJoin {
  investors: { name: string | null } | { name: string | null }[] | null;
}

interface ExportRow {
  name: string | null;
  slug: string | null;
  website: string | null;
  industry_group: string | null;
  hq_city: string | null;
  hq_state: string | null;
  latest_round_type: string | null;
  latest_round_amount: number | null;
  latest_round_date: string | null;
  total_raised_usd: number | null;
  employee_count_min: number | null;
  employee_count_max: number | null;
  company_investors: ExportInvestorJoin[] | null;
}

function rowToCsv(r: ExportRow): string {
  // Distinct investor names, comma-joined into a single cell.
  const names = new Set<string>();
  for (const ci of r.company_investors ?? []) {
    const inv = Array.isArray(ci.investors) ? ci.investors[0] : ci.investors;
    if (inv?.name) names.add(inv.name);
  }
  const cells = [
    r.name,
    r.slug,
    r.website,
    r.industry_group,
    r.hq_city,
    r.hq_state,
    r.latest_round_type,
    // Plain integer USD for the amount columns — the display "$15M" form is in
    // the UI; a CSV should carry the raw number so it's spreadsheet-usable.
    r.latest_round_amount != null ? Math.round(Number(r.latest_round_amount)) : null,
    r.latest_round_date,
    r.total_raised_usd != null ? Math.round(Number(r.total_raised_usd)) : null,
    r.employee_count_min,
    r.employee_count_max,
    [...names].join(", "),
  ];
  return cells.map(csvField).join(",");
}

export async function GET(request: Request): Promise<Response> {
  const params = new URL(request.url).searchParams;
  const opts = optionsFromParams(params);

  // The industry filter accepts the display label ("AI infrastructure") AND
  // the URL slug ("ai-infrastructure") — customers copy the slug straight out
  // of /industry URLs, and the silent zero-row export it used to produce was
  // a 2026-07 QA finding. An unresolvable value passes through unchanged
  // (same zero-row behavior as any unknown label).
  if (opts.industry_group) {
    const canonical = (await listCanonicalIndustries()).map((i) => i.group);
    if (!canonical.includes(opts.industry_group)) {
      const resolved = resolveIndustrySlug(opts.industry_group, canonical);
      if (resolved) opts.industry_group = resolved;
    }
  }

  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn("[export] Supabase not configured:", (err as Error).message);
    return new Response("Export unavailable: backend not configured.\n", {
      status: 503,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  }

  const selectCols =
    "slug, name, website, industry_group, hq_city, hq_state, " +
    "latest_round_type, latest_round_amount, latest_round_date, " +
    "total_raised_usd, employee_count_min, employee_count_max, " +
    "company_investors(investors(name))";

  const search = opts.search ? sanitizeIlikeTerm(opts.search) : "";

  // Stream the CSV: header first, then keyset-paginated rows. Building the body
  // lazily keeps memory flat for large exports.
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(encoder.encode(COLUMNS.join(",") + "\n"));

      let lastSlug: string | null = null;
      try {
        for (let page = 0; page < MAX_PAGES; page++) {
          let query = supabase
            .from("companies")
            .select(selectCols)
            .is("exclusion_reason", null)
            .or(CATALOG_BAR_OR)
            .order("slug", { ascending: true })
            .limit(PAGE_SIZE);

          if (search) {
            query = query.or(
              `name.ilike.%${search}%,description_short.ilike.%${search}%`,
            );
          }
          query = applyCompanyFilters(query, opts);
          if (lastSlug !== null) query = query.gt("slug", lastSlug);

          const { data, error } = await query;
          if (error) {
            console.error("[export] page query failed:", error.message);
            controller.enqueue(
              encoder.encode(`# export interrupted: ${error.message}\n`),
            );
            break;
          }

          const rows = (data ?? []) as unknown as ExportRow[];
          for (const r of rows) {
            controller.enqueue(encoder.encode(rowToCsv(r) + "\n"));
          }

          if (rows.length < PAGE_SIZE) break;
          lastSlug = rows[rows.length - 1].slug;
          if (lastSlug == null) break;
        }
      } catch (err) {
        console.error("[export] stream failed:", (err as Error).message);
        controller.enqueue(encoder.encode("# export interrupted\n"));
      }
      controller.close();
    },
  });

  return new Response(stream, {
    status: 200,
    headers: {
      "content-type": "text/csv; charset=utf-8",
      "content-disposition": 'attachment; filename="nous-companies.csv"',
      "cache-control": "no-store",
    },
  });
}
