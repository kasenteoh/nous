import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { createSupabaseServerClient } from "@/lib/db";
import {
  listRecentFundingsByIndustry,
  listRecentFundingsForCompanySlugs,
  listRecentNewsByIndustry,
  listRecentNewsForCompanySlugs,
} from "@/lib/queries";
import {
  createMockSupabase,
  type MockSupabase,
  type Responder,
} from "./helpers/mock-supabase";

vi.mock("@/lib/db", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/db")>();
  return { ...actual, createSupabaseServerClient: vi.fn() };
});

const mockedCreate = vi.mocked(createSupabaseServerClient);

function useClient(respond: Responder): MockSupabase {
  const mock = createMockSupabase(respond);
  mockedCreate.mockReturnValue(mock.client);
  return mock;
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

// A funding_rounds join row as PostgREST returns it (company as an object).
const fundingRow = {
  round_type: "Series A",
  amount_raised: 12_000_000,
  announced_date: "2026-05-01",
  companies: { name: "Acme", slug: "acme" },
};

// A news_articles join row.
const newsRow = {
  id: "n-1",
  title: "Acme raises",
  url: "https://news.test/acme",
  source: "TechCrunch",
  published_date: "2026-05-02",
  companies: { name: "Acme", slug: "acme" },
};

describe("listRecentFundingsByIndustry", () => {
  it("filters funding_rounds to the industry_group on the inner-joined company, dated, newest-first", async () => {
    const mock = useClient(() => ({ data: [fundingRow] }));
    const rows = await listRecentFundingsByIndustry("Fintech", 30);

    const b = mock.buildersFor("funding_rounds")[0];
    expect(b.has("is", "companies.exclusion_reason", null)).toBe(true);
    expect(b.has("eq", "companies.industry_group", "Fintech")).toBe(true);
    expect(b.has("not", "announced_date", "is", null)).toBe(true);
    expect(b.has("order", "announced_date", { ascending: false })).toBe(true);
    expect(b.has("limit", 30)).toBe(true);

    expect(rows).toEqual([
      {
        companySlug: "acme",
        companyName: "Acme",
        round_type: "Series A",
        amount_raised: 12_000_000,
        announced_date: "2026-05-01",
      },
    ]);
  });

  it("drops rows whose company join is missing", async () => {
    useClient(() => ({
      data: [fundingRow, { ...fundingRow, companies: null }],
    }));
    const rows = await listRecentFundingsByIndustry("Fintech");
    expect(rows).toHaveLength(1);
  });

  it("returns [] on a query error", async () => {
    useClient(() => ({ error: { message: "boom" } }));
    await expect(listRecentFundingsByIndustry("Fintech")).resolves.toEqual([]);
  });

  it("returns [] when Supabase is unconfigured (secret-free CI/dev)", async () => {
    mockedCreate.mockImplementation(() => {
      throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set");
    });
    await expect(listRecentFundingsByIndustry("Fintech")).resolves.toEqual([]);
  });
});

describe("listRecentNewsByIndustry", () => {
  it("filters news_articles to the industry_group, dated, newest-first", async () => {
    const mock = useClient(() => ({ data: [newsRow] }));
    const rows = await listRecentNewsByIndustry("Fintech", 30);

    const b = mock.buildersFor("news_articles")[0];
    expect(b.has("is", "companies.exclusion_reason", null)).toBe(true);
    expect(b.has("eq", "companies.industry_group", "Fintech")).toBe(true);
    expect(b.has("not", "published_date", "is", null)).toBe(true);
    expect(b.has("order", "published_date", { ascending: false })).toBe(true);

    expect(rows).toEqual([
      {
        id: "n-1",
        title: "Acme raises",
        url: "https://news.test/acme",
        source: "TechCrunch",
        published_date: "2026-05-02",
        companySlug: "acme",
        companyName: "Acme",
      },
    ]);
  });

  it("returns [] on a query error", async () => {
    useClient(() => ({ error: { message: "boom" } }));
    await expect(listRecentNewsByIndustry("Fintech")).resolves.toEqual([]);
  });
});

describe("listRecentFundingsForCompanySlugs", () => {
  it("filters funding_rounds to the slug set on the inner-joined company", async () => {
    const mock = useClient(() => ({ data: [fundingRow] }));
    const rows = await listRecentFundingsForCompanySlugs(["acme", "globex"], 30);

    const b = mock.buildersFor("funding_rounds")[0];
    expect(b.has("is", "companies.exclusion_reason", null)).toBe(true);
    expect(b.has("in", "companies.slug", ["acme", "globex"])).toBe(true);
    expect(b.has("not", "announced_date", "is", null)).toBe(true);
    expect(rows).toHaveLength(1);
  });

  it("short-circuits to [] for an empty slug set without querying", async () => {
    const mock = useClient(() => ({ data: [fundingRow] }));
    await expect(listRecentFundingsForCompanySlugs([])).resolves.toEqual([]);
    // No builder was ever created — the query never ran.
    expect(mock.builders).toHaveLength(0);
  });

  it("returns [] on a query error", async () => {
    useClient(() => ({ error: { message: "boom" } }));
    await expect(
      listRecentFundingsForCompanySlugs(["acme"]),
    ).resolves.toEqual([]);
  });
});

describe("listRecentNewsForCompanySlugs", () => {
  it("filters news_articles to the slug set on the inner-joined company", async () => {
    const mock = useClient(() => ({ data: [newsRow] }));
    const rows = await listRecentNewsForCompanySlugs(["acme"], 30);

    const b = mock.buildersFor("news_articles")[0];
    expect(b.has("in", "companies.slug", ["acme"])).toBe(true);
    expect(b.has("not", "published_date", "is", null)).toBe(true);
    expect(rows).toHaveLength(1);
  });

  it("short-circuits to [] for an empty slug set without querying", async () => {
    const mock = useClient(() => ({ data: [newsRow] }));
    await expect(listRecentNewsForCompanySlugs([])).resolves.toEqual([]);
    expect(mock.builders).toHaveLength(0);
  });
});
