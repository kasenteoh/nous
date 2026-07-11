import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { createSupabaseServerClient, SupabaseConfigError } from "@/lib/db";
import {
  applyCompanyFilters,
  CATALOG_BAR_OR,
  countCompanies,
  getAliasTargetSlug,
  getAlsoBackedBy,
  getAlternatives,
  getCompaniesForCompare,
  getCompanyBySlug,
  getCompanyOgData,
  getRelatedCompanies,
  listCompanies,
  listNewestCompanies,
  sanitizeIlikeTerm,
  searchHuskFallback,
  type CompanyFilterable,
} from "@/lib/queries";
import {
  createMockSupabase,
  type MockQueryBuilder,
  type MockSupabase,
  type Responder,
} from "./helpers/mock-supabase";

vi.mock("@/lib/db", async (importOriginal) => {
  // Keep the real SupabaseConfigError so queries.ts's instanceof rethrow
  // check works against the class the tests (and prod code) throw.
  const actual = await importOriginal<typeof import("@/lib/db")>();
  return { ...actual, createSupabaseServerClient: vi.fn() };
});

const mockedCreate = vi.mocked(createSupabaseServerClient);

/** Wire a mock client whose every awaited query resolves via `respond`. */
function useClient(respond: Responder): MockSupabase {
  const mock = createMockSupabase(respond);
  mockedCreate.mockReturnValue(mock.client);
  return mock;
}

beforeEach(() => {
  // The code under test logs expected degradations; keep test output clean.
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// ─── sanitizeIlikeTerm ─────────────────────────────────────────────────────────

describe("sanitizeIlikeTerm", () => {
  it("strips PostgREST wildcard characters (% and *)", () => {
    expect(sanitizeIlikeTerm("acme%corp")).toBe("acme corp");
    expect(sanitizeIlikeTerm("*acme*")).toBe("acme");
  });

  it("strips the .or() grammar separators (commas and parens) so a term can't inject clauses", () => {
    expect(sanitizeIlikeTerm("a,name.ilike.%x%")).toBe("a name.ilike. x");
    expect(sanitizeIlikeTerm("or(and(x))")).toBe("or and x");
  });

  it("strips backslashes", () => {
    expect(sanitizeIlikeTerm("a\\b")).toBe("a b");
  });

  it("collapses whitespace and trims", () => {
    expect(sanitizeIlikeTerm("  acme   corp  ")).toBe("acme corp");
    expect(sanitizeIlikeTerm("%%%")).toBe("");
  });

  it("leaves ordinary terms untouched", () => {
    expect(sanitizeIlikeTerm("acme corp")).toBe("acme corp");
  });

  it("currently passes the single-char wildcard _ through (documented behavior)", () => {
    // `_` matches any single character in ilike; the sanitizer only strips the
    // grammar-breaking set. This pins today's behavior — tighten deliberately.
    expect(sanitizeIlikeTerm("a_b")).toBe("a_b");
  });
});

// ─── applyCompanyFilters ───────────────────────────────────────────────────────

/** Bare recorder implementing the CompanyFilterable seam. */
class FilterRecorder implements CompanyFilterable {
  readonly calls: { method: string; args: unknown[] }[] = [];
  private rec(method: string, args: unknown[]): this {
    this.calls.push({ method, args });
    return this;
  }
  or(filters: string): this {
    return this.rec("or", [filters]);
  }
  eq(column: string, value: string): this {
    return this.rec("eq", [column, value]);
  }
  gte(column: string, value: string | number): this {
    return this.rec("gte", [column, value]);
  }
  lte(column: string, value: string | number): this {
    return this.rec("lte", [column, value]);
  }
  contains(column: string, value: readonly string[]): this {
    return this.rec("contains", [column, value]);
  }
}

describe("applyCompanyFilters", () => {
  it("maps every option onto the right column and operator", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-10T12:00:00Z"));

    const q = new FilterRecorder();
    applyCompanyFilters(q, {
      industry_group: "Fintech",
      discovered_via: "techcrunch",
      tag: "ai",
      state: "CA",
      min_raised: 1_000_000,
      max_raised: 50_000_000,
      founded_after: 2019,
      founded_before: 2024,
      emp_min: 10,
      emp_max: 200,
      stage: "Series A",
      funded_since_days: 30,
    });

    expect(q.calls).toEqual([
      { method: "eq", args: ["industry_group", "Fintech"] },
      { method: "eq", args: ["discovered_via", "techcrunch"] },
      // Exact array containment — never a substring match ("ai" must not
      // conflate with "ai-infrastructure").
      { method: "contains", args: ["tags", ["ai"]] },
      { method: "eq", args: ["hq_state", "CA"] },
      { method: "gte", args: ["total_raised_usd", 1_000_000] },
      { method: "lte", args: ["total_raised_usd", 50_000_000] },
      { method: "gte", args: ["year_incorporated", 2019] },
      { method: "lte", args: ["year_incorporated", 2024] },
      // Headcount is a range: "at least 10" checks the UPPER bound reaches 10,
      // "at most 200" checks the LOWER bound is within 200.
      { method: "gte", args: ["employee_count_max", 10] },
      { method: "lte", args: ["employee_count_min", 200] },
      { method: "eq", args: ["latest_round_type", "Series A"] },
      { method: "gte", args: ["latest_round_date", "2026-06-10"] },
    ]);
  });

  it("applies nothing for empty options", () => {
    const q = new FilterRecorder();
    applyCompanyFilters(q, {});
    expect(q.calls).toEqual([]);
  });

  it("ignores a non-positive funded_since_days", () => {
    const q = new FilterRecorder();
    applyCompanyFilters(q, { funded_since_days: 0 });
    expect(q.calls).toEqual([]);
  });
});

// ─── listCompanies ─────────────────────────────────────────────────────────────

const LIST_ROW = {
  slug: "acme",
  name: "Acme",
  hq_city: "Austin",
  hq_state: "TX",
  industry_group: "DevTools",
  description_short: "Tools.",
  status: "active",
  logo_url: null,
};

describe("listCompanies", () => {
  it("applies the exclusion filter and the catalog bar on the browse surface", async () => {
    const mock = useClient(() => ({ data: [LIST_ROW], count: 1 }));
    await listCompanies({});

    const main = mock.buildersFor("companies")[0];
    expect(main.has("is", "exclusion_reason", null)).toBe(true);
    expect(main.has("or", CATALOG_BAR_OR)).toBe(true);
    expect(main.has("range", 0, 29)).toBe(true); // default page size 30
  });

  it("sanitizes the search term before building the name/description .or()", async () => {
    const mock = useClient(() => ({ data: [], count: 0 }));
    await listCompanies({ search: "acme%,co(rp)" });

    const main = mock.buildersFor("companies")[0];
    expect(
      main.has(
        "or",
        "name.ilike.%acme co rp%,description_short.ilike.%acme co rp%",
      ),
    ).toBe(true);
  });

  it("returns rows plus the exact total from the count header", async () => {
    useClient(() => ({ data: [LIST_ROW], count: 137 }));
    const result = await listCompanies({});
    expect(result.total).toBe(137);
    expect(result.rows).toEqual([LIST_ROW]);
  });

  it("clamps an out-of-range page (PGRST103) to rows=[] with the real total when the count survived", async () => {
    useClient(() => ({
      error: { message: "Requested range not satisfiable", code: "PGRST103" },
      count: 42,
    }));
    const result = await listCompanies({ offset: 9000 });
    expect(result).toEqual({ rows: [], total: 42 });
  });

  it("falls back to a head-only count with identical filters when PGRST103 loses the count", async () => {
    const mock = useClient((b) => {
      if (b.has("range", 9000, 9029)) {
        return {
          error: {
            message: "Requested range not satisfiable",
            code: "PGRST103",
          },
          count: null,
        };
      }
      return { count: 87 }; // the fallback head-only count
    });

    const result = await listCompanies({
      offset: 9000,
      industry_group: "Fintech",
      search: "acme",
    });
    expect(result).toEqual({ rows: [], total: 87 });

    // The fallback count query must re-apply the same filter semantics so it
    // can never drift from the main query.
    const fallback = mock.buildersFor("companies")[1];
    expect(fallback.has("is", "exclusion_reason", null)).toBe(true);
    expect(fallback.has("or", CATALOG_BAR_OR)).toBe(true);
    expect(fallback.has("eq", "industry_group", "Fintech")).toBe(true);
    expect(
      fallback.has(
        "or",
        "name.ilike.%acme%,description_short.ilike.%acme%",
      ),
    ).toBe(true);
  });

  it("returns total 0 when both the page query and the fallback count fail", async () => {
    useClient(() => ({ error: { message: "boom" }, count: null }));
    const result = await listCompanies({});
    expect(result).toEqual({ rows: [], total: 0 });
  });

  it("returns empty when Supabase is unconfigured (build-time prerender)", async () => {
    mockedCreate.mockImplementation(() => {
      throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set");
    });
    const result = await listCompanies({});
    expect(result).toEqual({ rows: [], total: 0 });
  });
});

describe("catalog bar on other list surfaces", () => {
  it("listNewestCompanies applies exclusion + catalog bar", async () => {
    const mock = useClient(() => ({ data: [] }));
    await listNewestCompanies();
    const b = mock.buildersFor("companies")[0];
    expect(b.has("is", "exclusion_reason", null)).toBe(true);
    expect(b.has("or", CATALOG_BAR_OR)).toBe(true);
  });

  it("countCompanies counts only catalog-bar companies", async () => {
    const mock = useClient(() => ({ count: 12 }));
    await expect(countCompanies()).resolves.toBe(12);
    const b = mock.buildersFor("companies")[0];
    expect(b.has("is", "exclusion_reason", null)).toBe(true);
    expect(b.has("or", CATALOG_BAR_OR)).toBe(true);
  });
});

// ─── searchHuskFallback ────────────────────────────────────────────────────────

describe("searchHuskFallback", () => {
  it("never queries when the term sanitizes to nothing", async () => {
    const mock = useClient(() => ({ data: [] }));
    await expect(searchHuskFallback("%()*,")).resolves.toEqual([]);
    expect(mock.builders).toHaveLength(0);
  });

  it("ilikes on the sanitized name and keeps the exclusion filter (no catalog bar — husks by design)", async () => {
    const mock = useClient(() => ({
      data: [
        { slug: "anthropic", name: "Anthropic" },
        { slug: null, name: "Broken" }, // unresolved row dropped
      ],
    }));
    const rows = await searchHuskFallback("anthro%pic");
    expect(rows).toEqual([{ slug: "anthropic", name: "Anthropic" }]);

    const b = mock.buildersFor("companies")[0];
    expect(b.has("is", "exclusion_reason", null)).toBe(true);
    expect(b.has("ilike", "name", "%anthro pic%")).toBe(true);
    expect(b.has("or", CATALOG_BAR_OR)).toBe(false);
  });
});

// ─── getCompanyBySlug ──────────────────────────────────────────────────────────

/** Minimal companies row for the single() fetch; queries.ts treats it as CompanyRow. */
function companyRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "c-main",
    slug: "acme",
    name: "Acme",
    exclusion_reason: null,
    ...overrides,
  };
}

describe("getCompanyBySlug", () => {
  it("returns null for an excluded company without fanning out to the detail queries", async () => {
    const mock = useClient((b) => {
      if (b.table === "companies") {
        return { data: companyRow({ exclusion_reason: "junk_page" }) };
      }
      throw new Error(`unexpected query on ${b.table}`);
    });

    await expect(getCompanyBySlug("acme")).resolves.toBeNull();
    expect(mock.buildersFor("people")).toHaveLength(0);
    expect(mock.buildersFor("funding_rounds")).toHaveLength(0);
  });

  it("returns null for an unknown slug (PGRST116 no-rows)", async () => {
    useClient(() => ({
      error: { message: "no rows", code: "PGRST116" },
    }));
    await expect(getCompanyBySlug("nope")).resolves.toBeNull();
  });

  it("shapes rounds (lead/other split, date-desc nulls-last) and null-outs excluded resolved competitors", async () => {
    useClient((b) => {
      switch (b.table) {
        case "companies":
          return { data: companyRow() };
        case "people":
          return { data: [] };
        case "funding_rounds":
          return {
            data: [
              {
                id: "r-old",
                announced_date: "2026-01-15",
                funding_round_investors: [
                  { is_lead: true, investors: { name: "Lead Fund" } },
                  // PostgREST may hand the embed back as a 1-element array.
                  { is_lead: false, investors: [{ name: "Other Fund" }] },
                  { is_lead: false, investors: null }, // unresolved → dropped
                  { is_lead: true, investors: { name: null } }, // nameless → dropped
                ],
              },
              { id: "r-undated", announced_date: null, funding_round_investors: null },
              { id: "r-new", announced_date: "2026-03-01", funding_round_investors: [] },
            ],
          };
        case "competitors":
          return {
            data: [
              {
                id: "comp-resolved",
                competitor_name: "Rival",
                rank: 1,
                competitor_company: { slug: "rival", name: "Rival Inc" },
              },
              {
                id: "comp-excluded",
                competitor_name: "Junk Co",
                rank: 2,
                // Resolved to an EXCLUDED company → must fall back to unlinked.
                competitor_company: [
                  { slug: "junk", name: "Junk Co", exclusion_reason: "junk" },
                ],
              },
              {
                id: "comp-unresolved",
                competitor_name: "Ghost",
                rank: 3,
                competitor_company: null,
              },
            ],
          };
        case "company_investors":
          return {
            data: [
              {
                is_lead: true,
                source: "vc_portfolio",
                investors: { name: "Lead Fund", website: "https://lead.example" },
              },
              { is_lead: null, source: null, investors: [{ name: "Array Fund", website: null }] },
              { is_lead: false, source: "x", investors: { name: null, website: null } },
            ],
          };
        case "news_articles":
          return { data: [] };
        default:
          throw new Error(`unexpected query on ${b.table}`);
      }
    });

    const detail = await getCompanyBySlug("acme");
    expect(detail).not.toBeNull();

    // Rounds sorted newest first, undated last; investor names split by role.
    expect(detail?.fundingRounds.map((r) => r.id)).toEqual([
      "r-new",
      "r-old",
      "r-undated",
    ]);
    const oldRound = detail?.fundingRounds.find((r) => r.id === "r-old");
    expect(oldRound?.leadInvestors).toEqual(["Lead Fund"]);
    expect(oldRound?.otherInvestors).toEqual(["Other Fund"]);

    // Competitors: resolved link only for a listable company.
    const byId = new Map(detail?.competitors.map((c) => [c.id, c]));
    expect(byId.get("comp-resolved")?.resolved).toEqual({
      slug: "rival",
      name: "Rival Inc",
    });
    expect(byId.get("comp-excluded")?.resolved).toBeNull();
    expect(byId.get("comp-unresolved")?.resolved).toBeNull();

    // Company-level investors flattened; nameless joins dropped.
    expect(detail?.investors).toEqual([
      {
        name: "Lead Fund",
        website: "https://lead.example",
        isLead: true,
        source: "vc_portfolio",
      },
      { name: "Array Fund", website: null, isLead: false, source: "" },
    ]);
  });
});

// ─── excluded-company null-out on the other surfacing paths ────────────────────

describe("excluded-company null-out elsewhere", () => {
  it("getAlternatives returns null for an excluded subject company", async () => {
    useClient((b) => {
      if (b.table === "companies") {
        return { data: companyRow({ exclusion_reason: "junk" }) };
      }
      throw new Error(`unexpected query on ${b.table}`);
    });
    await expect(getAlternatives("acme")).resolves.toBeNull();
  });

  it("getCompanyOgData returns null for an excluded company", async () => {
    useClient(() => ({
      data: { name: "Acme", industry_group: null, exclusion_reason: "junk" },
    }));
    await expect(getCompanyOgData("acme")).resolves.toBeNull();
  });

  it("getRelatedCompanies drops related companies that are excluded or unresolved", async () => {
    useClient(() => ({
      data: [
        {
          score: 0.9,
          evidence: "shared tags",
          related_company: { slug: "ok", name: "OK Co", exclusion_reason: null },
        },
        {
          score: 0.8,
          evidence: null,
          related_company: { slug: "bad", name: "Bad Co", exclusion_reason: "junk" },
        },
        { score: 0.7, evidence: null, related_company: null },
      ],
    }));
    const related = await getRelatedCompanies("c-main");
    expect(related.map((r) => r.slug)).toEqual(["ok"]);
  });

  it("getCompaniesForCompare drops excluded slugs and preserves the caller's order", async () => {
    useClient(() => ({
      data: [
        // Returned out of order, one excluded — output must follow input order.
        {
          slug: "bravo",
          name: "Bravo",
          total_raised_usd: 1_000_000,
          funding_rounds: [
            { amount_raised: 2_000_000, funding_round_investors: [] },
          ],
          company_investors: [],
          competitors: [],
        },
        {
          slug: "junk",
          name: "Junk",
          exclusion_reason: "junk",
          funding_rounds: [],
          company_investors: [],
          competitors: [],
        },
        {
          slug: "alpha",
          name: "Alpha",
          funding_rounds: [],
          company_investors: [],
          competitors: [],
        },
      ],
    }));

    const companies = await getCompaniesForCompare(["alpha", "junk", "bravo"]);
    expect(companies.map((c) => c.slug)).toEqual(["alpha", "bravo"]);
    // Hybrid total: max(stated $1M, computed $2M) = $2M.
    expect(companies[1].totalRaised).toBe(2_000_000);
  });
});

// ─── getAlternatives meta-leak + split ─────────────────────────────────────────

describe("getAlternatives", () => {
  it("drops meta-leak rows and splits the rest into resolved vs named", async () => {
    useClient((b) => {
      if (b.table === "companies") {
        return {
          data: {
            id: "c-main",
            slug: "acme",
            name: "Acme",
            description_short: "Tools.",
            industry_group: "DevTools",
            exclusion_reason: null,
          },
        };
      }
      if (b.table === "competitors") {
        return {
          data: [
            {
              competitor_name: "Leaked",
              rank: 1,
              reasoning:
                "Included temporarily for evaluation but should be dropped.",
              description: null,
              source: "llm_inferred",
              source_url: null,
              competitor_company: null,
            },
            {
              competitor_name: "Rival Inc",
              rank: 2,
              reasoning: "Same market.",
              description: null,
              source: "techcrunch",
              source_url: "https://techcrunch.com/x",
              competitor_company: {
                slug: "rival",
                name: "Rival Inc",
                hq_city: null,
                hq_state: null,
                industry_group: null,
                description_short: null,
                status: "active",
                logo_url: null,
                exclusion_reason: null,
              },
            },
            {
              competitor_name: "Ghost Co",
              rank: 3,
              reasoning: "Adjacent product.",
              description: null,
              source: "llm_inferred",
              source_url: null,
              competitor_company: {
                slug: "ghost",
                name: "Ghost Co",
                hq_city: null,
                hq_state: null,
                industry_group: null,
                description_short: null,
                status: "active",
                logo_url: null,
                exclusion_reason: "junk", // resolved-but-excluded → named
              },
            },
          ],
        };
      }
      throw new Error(`unexpected query on ${b.table}`);
    });

    const data = await getAlternatives("acme");
    expect(data).not.toBeNull();
    expect(data?.resolved.map((r) => r.slug)).toEqual(["rival"]);
    expect(data?.named.map((n) => n.name)).toEqual(["Ghost Co"]);
    // The leaked scratch-note row appears in NEITHER list.
    const everyName = [
      ...(data?.resolved.map((r) => r.name) ?? []),
      ...(data?.named.map((n) => n.name) ?? []),
    ];
    expect(everyName).not.toContain("Leaked");
  });
});

// ─── getAlsoBackedBy ───────────────────────────────────────────────────────────

describe("getAlsoBackedBy", () => {
  const COMPANY = "c-main";

  /** Degree counts per investor for the head-only count queries. */
  const DEGREES: Record<string, { ci: number; fri: number }> = {
    "inv-a": { ci: 2, fri: 0 },
    "inv-b": { ci: 1, fri: 2 },
    "inv-mega": { ci: 20, fri: 15 }, // 35 > 30 → high-degree, excluded
  };

  function degreeOf(b: MockQueryBuilder): { ci: number; fri: number } | null {
    const eq = b.argsOf("eq").find((args) => args[0] === "investor_id");
    if (!eq) return null;
    return DEGREES[eq[1] as string] ?? null;
  }

  const respond: Responder = (b) => {
    if (b.table === "company_investors") {
      // Step 1a: this company's direct investors.
      if (b.has("eq", "company_id", COMPANY)) {
        return { data: [{ investor_id: "inv-a" }] };
      }
      // Step 3a: companies backed by the surviving low-degree investors.
      if (b.argsOf("in").length > 0) {
        return {
          data: [
            { company_id: "c-bravo", investor_id: "inv-a" },
            { company_id: "c-charlie", investor_id: "inv-a" },
          ],
        };
      }
      // Step 2: per-investor degree count (head-only).
      const d = degreeOf(b);
      if (d) return { count: d.ci };
    }
    if (b.table === "funding_round_investors") {
      // Step 1b: round-level investors joined through funding_rounds.
      if (b.has("eq", "funding_rounds.company_id", COMPANY)) {
        return {
          data: [
            { investor_id: "inv-b", funding_rounds: { company_id: COMPANY } },
            { investor_id: "inv-mega", funding_rounds: { company_id: COMPANY } },
          ],
        };
      }
      if (b.argsOf("in").length > 0) {
        return {
          data: [
            { investor_id: "inv-b", funding_rounds: { company_id: "c-bravo" } },
            { investor_id: "inv-b", funding_rounds: { company_id: "c-delta" } },
            // Self-reference must be skipped, not counted.
            { investor_id: "inv-a", funding_rounds: { company_id: COMPANY } },
          ],
        };
      }
      const d = degreeOf(b);
      if (d) return { count: d.fri };
    }
    if (b.table === "investors") {
      return {
        data: [
          { id: "inv-a", name: "Alpha Capital" },
          { id: "inv-b", name: "Beta Ventures" },
        ],
      };
    }
    if (b.table === "companies") {
      return {
        data: [
          { id: "c-bravo", slug: "bravo", name: "Bravo" },
          { id: "c-charlie", slug: "charlie", name: "Charlie" },
          // Excluded → dead /c/[slug] link → dropped from the result.
          { id: "c-delta", slug: "delta", name: "Delta", exclusion_reason: "junk" },
        ],
      };
    }
    throw new Error(`unexpected query on ${b.table}`);
  };

  it("unions both investor paths, excludes the high-degree fund, ranks by shared count, and drops excluded companies", async () => {
    const mock = useClient(respond);
    const result = await getAlsoBackedBy(COMPANY);

    expect(result).toEqual([
      // c-bravo shares BOTH low-degree investors (2) → first; names sorted.
      {
        slug: "bravo",
        name: "Bravo",
        sharedInvestors: ["Alpha Capital", "Beta Ventures"],
      },
      // c-charlie and c-delta both share 1, but delta is excluded → dropped.
      { slug: "charlie", name: "Charlie", sharedInvestors: ["Alpha Capital"] },
    ]);

    // The mega-fund must never reach the shared-portfolio step: every .in()
    // on the edge tables carries only the two low-degree ids.
    for (const table of ["company_investors", "funding_round_investors"]) {
      const sharedIns = mock
        .buildersFor(table)
        .flatMap((b) => b.argsOf("in"))
        .filter((args) => args[0] === "investor_id");
      expect(sharedIns).toEqual([["investor_id", ["inv-a", "inv-b"]]]);
    }
  });

  it("returns [] when the company has no recorded investors, without degree queries", async () => {
    const mock = useClient((b) => {
      if (
        b.table === "company_investors" ||
        b.table === "funding_round_investors"
      ) {
        return { data: [] };
      }
      throw new Error(`unexpected query on ${b.table}`);
    });
    await expect(getAlsoBackedBy(COMPANY)).resolves.toEqual([]);
    // Only the two step-1 queries ran.
    expect(mock.builders).toHaveLength(2);
  });

  it("returns [] when every investor is high-degree", async () => {
    const mock = useClient((b) => {
      if (b.table === "company_investors") {
        if (b.has("eq", "company_id", COMPANY)) {
          return { data: [{ investor_id: "inv-mega" }] };
        }
        return { count: 40 };
      }
      if (b.table === "funding_round_investors") {
        if (b.has("eq", "funding_rounds.company_id", COMPANY)) {
          return { data: [] };
        }
        return { count: 40 };
      }
      throw new Error(`unexpected query on ${b.table}`);
    });
    await expect(getAlsoBackedBy(COMPANY)).resolves.toEqual([]);
    expect(mock.buildersFor("investors")).toHaveLength(0);
  });

  it("treats an unknown degree (count error) as high-degree rather than relating half the catalog", async () => {
    const mock = useClient((b) => {
      if (b.table === "company_investors") {
        if (b.has("eq", "company_id", COMPANY)) {
          return { data: [{ investor_id: "inv-a" }] };
        }
        return { error: { message: "transient" } }; // degree count fails
      }
      if (b.table === "funding_round_investors") {
        if (b.has("eq", "funding_rounds.company_id", COMPANY)) {
          return { data: [] };
        }
        return { count: 1 };
      }
      throw new Error(`unexpected query on ${b.table}`);
    });
    await expect(getAlsoBackedBy(COMPANY)).resolves.toEqual([]);
    expect(mock.buildersFor("investors")).toHaveLength(0);
  });
});

// ─── W-C.2: env-missing behavior ──────────────────────────────────────────────

describe("Supabase config failures", () => {
  it("degrades to empty on a benign not-configured error (secret-free CI/dev)", async () => {
    mockedCreate.mockImplementation(() => {
      throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set");
    });
    await expect(listCompanies({})).resolves.toEqual({ rows: [], total: 0 });
    await expect(getCompanyBySlug("acme")).resolves.toBeNull();
  });

  it("rethrows SupabaseConfigError so a Vercel misconfig 500s instead of 404ing", async () => {
    mockedCreate.mockImplementation(() => {
      throw new SupabaseConfigError("SUPABASE_URL not set in the Vercel environment");
    });
    await expect(listCompanies({})).rejects.toBeInstanceOf(SupabaseConfigError);
    await expect(getCompanyBySlug("acme")).rejects.toBeInstanceOf(
      SupabaseConfigError,
    );
  });
});

// ─── W-E.4: slug-alias lookup (permanent redirects for merged-away slugs) ─────

describe("getAliasTargetSlug", () => {
  it("returns the survivor's current slug for an aliased slug (object embed)", async () => {
    const mock = useClient(() => ({
      data: { companies: { slug: "acme" } },
    }));
    await expect(getAliasTargetSlug("acme-inc")).resolves.toBe("acme");

    // One single-row query keyed by old_slug, slug embedded via the FK.
    const [b] = mock.buildersFor("slug_aliases");
    expect(b.has("select", "companies!company_id(slug)")).toBe(true);
    expect(b.has("eq", "old_slug", "acme-inc")).toBe(true);
    expect(b.has("single")).toBe(true);
    expect(mock.builders).toHaveLength(1);
  });

  it("normalizes a single-element-array embed (PostgREST cardinality quirk)", async () => {
    useClient(() => ({ data: { companies: [{ slug: "acme" }] } }));
    await expect(getAliasTargetSlug("acme-inc")).resolves.toBe("acme");
  });

  it("returns null on a miss (PGRST116) without logging — the expected 404 path", async () => {
    useClient(() => ({
      data: null,
      error: { message: "JSON object requested, multiple (or no) rows returned", code: "PGRST116" },
    }));
    await expect(getAliasTargetSlug("never-existed")).resolves.toBeNull();
    expect(console.error).not.toHaveBeenCalled();
  });

  it("returns null and logs on an unexpected error (e.g. table missing pre-0032)", async () => {
    useClient(() => ({
      data: null,
      error: { message: 'relation "slug_aliases" does not exist', code: "42P01" },
    }));
    await expect(getAliasTargetSlug("acme-inc")).resolves.toBeNull();
    expect(console.error).toHaveBeenCalled();
  });

  it("returns null when the embed dangles (alias row without a resolvable company)", async () => {
    useClient(() => ({ data: { companies: null } }));
    await expect(getAliasTargetSlug("acme-inc")).resolves.toBeNull();
  });

  it("returns null when Supabase is unconfigured (secret-free CI/dev)", async () => {
    mockedCreate.mockImplementation(() => {
      throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set");
    });
    await expect(getAliasTargetSlug("acme-inc")).resolves.toBeNull();
  });
});
