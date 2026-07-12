// Query-layer tests for the /themes surfaces (Wave 3 E-3): listThemes,
// getThemeBySlug, listAllThemeSlugs — at the same observable boundary as
// queries.test.ts (which filters were applied, what the code does with the
// scripted response).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSupabaseServerClient } from "@/lib/db";
import {
  getThemeBySlug,
  listAllThemeSlugs,
  listThemes,
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

// ─── Fixtures ─────────────────────────────────────────────────────────────────

const THEME_ROW = {
  id: "theme-1",
  slug: "agentic-coding-tools",
  name: "Agentic Coding Tools",
  industry_group: "DevTools",
  description: "Tools that write and review code with AI agents.",
  company_count: 4,
  funding_recent_usd: "15000000",
  funding_prior_usd: "5000000",
  funding_growth: "2.0000",
  updated_at: "2026-07-01T00:00:00Z",
};

function memberRow(
  slug: string,
  similarity: number,
  overrides: Record<string, unknown> = {},
) {
  return {
    similarity,
    companies: {
      slug,
      name: `Co ${slug}`,
      hq_city: "Austin",
      hq_state: "TX",
      industry_group: "DevTools",
      description_short: "Builds things.",
      status: "active",
      logo_url: null,
      created_at: "2026-06-01T00:00:00Z",
      exclusion_reason: null,
      ...overrides,
    },
  };
}

// ─── listThemes ───────────────────────────────────────────────────────────────

describe("listThemes", () => {
  it("ranks by funding growth (NULLS LAST), then recent funding, then name", async () => {
    const mock = useClient(() => ({ data: [THEME_ROW] }));
    await listThemes();

    const main = mock.buildersFor("themes")[0];
    expect(
      main.has("order", "funding_growth", { ascending: false, nullsFirst: false }),
    ).toBe(true);
    expect(main.has("order", "funding_recent_usd", { ascending: false })).toBe(
      true,
    );
    expect(main.has("order", "name", { ascending: true })).toBe(true);
  });

  it("coerces numerics and preserves a null growth (zero prior base)", async () => {
    useClient(() => ({
      data: [
        THEME_ROW,
        { ...THEME_ROW, slug: "new-theme", name: "New", funding_growth: null },
      ],
    }));
    const rows = await listThemes();

    expect(rows).toHaveLength(2);
    expect(rows[0].funding_recent_usd).toBe(15_000_000);
    expect(rows[0].funding_growth).toBe(2);
    expect(rows[1].funding_growth).toBeNull();
  });

  it("drops malformed rows and degrades to [] on error / missing env", async () => {
    useClient(() => ({ data: [{ ...THEME_ROW, slug: null }] }));
    expect(await listThemes()).toEqual([]);

    useClient(() => ({ error: { message: "boom" } }));
    expect(await listThemes()).toEqual([]);

    mockedCreate.mockImplementation(() => {
      throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set");
    });
    expect(await listThemes()).toEqual([]);
  });
});

// ─── getThemeBySlug ───────────────────────────────────────────────────────────

describe("getThemeBySlug", () => {
  function respondFor(
    members: unknown[],
    rounds: unknown[] = [],
  ): Responder {
    return (b) => {
      if (b.table === "themes") return { data: THEME_ROW };
      if (b.table === "company_themes") return { data: members };
      if (b.table === "funding_rounds") return { data: rounds };
      throw new Error(`unexpected table ${b.table}`);
    };
  }

  it("returns null for an unknown slug (PGRST116) → 404", async () => {
    useClient(() => ({
      error: { message: "0 rows", code: "PGRST116" },
    }));
    expect(await getThemeBySlug("nope")).toBeNull();
  });

  it("orders members by similarity and drops excluded companies", async () => {
    const mock = useClient(
      respondFor([
        memberRow("alpha", 0.97),
        memberRow("hidden", 0.95, { exclusion_reason: "not_a_startup" }),
        memberRow("beta", 0.91),
      ]),
    );

    const data = await getThemeBySlug("agentic-coding-tools");
    expect(data).not.toBeNull();
    expect(data!.members.map((m) => m.slug)).toEqual(["alpha", "beta"]);
    expect(data!.members[0].similarity).toBe(0.97);
    expect(data!.members[0].created_at).toBe("2026-06-01T00:00:00Z");

    const membersQuery = mock.buildersFor("company_themes")[0];
    expect(membersQuery.has("eq", "theme_id", "theme-1")).toBe(true);
    expect(
      membersQuery.has("order", "similarity", { ascending: false }),
    ).toBe(true);
  });

  it("fetches member rounds by slug and coerces amounts", async () => {
    const mock = useClient(
      respondFor(
        [memberRow("alpha", 0.97), memberRow("beta", 0.91)],
        [
          { announced_date: "2026-03-01", amount_raised: "10000000" },
          { announced_date: null, amount_raised: "5000000" },
        ],
      ),
    );

    const data = await getThemeBySlug("agentic-coding-tools");
    expect(data!.rounds).toEqual([
      { announced_date: "2026-03-01", amount_raised: 10_000_000 },
      { announced_date: null, amount_raised: 5_000_000 },
    ]);

    const roundsQuery = mock.buildersFor("funding_rounds")[0];
    // Excluded members never reach this list, so their rounds are never
    // aggregated either (the fixture members here are both shown).
    expect(
      roundsQuery.has("in", "companies.slug", ["alpha", "beta"]),
    ).toBe(true);
  });

  it("skips the rounds query entirely when no member survives", async () => {
    const mock = useClient(
      respondFor([
        memberRow("hidden", 0.95, { exclusion_reason: "manual" }),
      ]),
    );

    const data = await getThemeBySlug("agentic-coding-tools");
    expect(data!.members).toEqual([]);
    expect(data!.rounds).toEqual([]);
    expect(mock.buildersFor("funding_rounds")).toHaveLength(0);
  });

  it("still returns the theme header when the members query fails", async () => {
    useClient((b) => {
      if (b.table === "themes") return { data: THEME_ROW };
      return { error: { message: "boom" } };
    });

    const data = await getThemeBySlug("agentic-coding-tools");
    expect(data).not.toBeNull();
    expect(data!.theme.name).toBe("Agentic Coding Tools");
    expect(data!.members).toEqual([]);
  });
});

// ─── listAllThemeSlugs (sitemap) ──────────────────────────────────────────────

describe("listAllThemeSlugs", () => {
  it("applies the ≥3-member threshold (alternatives-pattern de-thinning)", async () => {
    const mock = useClient(() => ({
      data: [{ slug: "big-theme", updated_at: "2026-07-01T00:00:00Z" }],
    }));
    const rows = await listAllThemeSlugs();

    const main = mock.buildersFor("themes")[0];
    expect(main.has("gte", "company_count", 3)).toBe(true);
    expect(rows).toEqual([
      { slug: "big-theme", updated_at: "2026-07-01T00:00:00Z" },
    ]);
  });

  it("degrades to [] on error so the sitemap still builds", async () => {
    useClient(() => ({ error: { message: "boom" } }));
    expect(await listAllThemeSlugs()).toEqual([]);
  });
});
