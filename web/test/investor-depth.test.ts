import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { createSupabaseServerClient } from "@/lib/db";
import { getInvestorPortfolioMomentum } from "@/lib/queries";
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
});

afterEach(() => {
  vi.restoreAllMocks();
});

// Dispatch a scripted response per table so the investor lookup + the two
// portfolio link-path queries each get their own data.
function byTable(map: {
  investors?: unknown;
  company_investors?: unknown;
  funding_round_investors?: unknown;
  investorsError?: { message: string; code?: string };
}): Responder {
  return (b) => {
    if (b.table === "investors") {
      return map.investorsError
        ? { error: map.investorsError }
        : { data: map.investors ?? { id: "inv-1" } };
    }
    if (b.table === "company_investors") return { data: map.company_investors ?? [] };
    if (b.table === "funding_round_investors")
      return { data: map.funding_round_investors ?? [] };
    return { data: [] };
  };
}

function co(
  slug: string,
  momentum_score: number | null,
  extra: Partial<{ momentum_why: string[]; exclusion_reason: string | null }> = {},
) {
  return {
    slug,
    name: slug.toUpperCase(),
    momentum_score,
    momentum_why: extra.momentum_why ?? [],
    exclusion_reason: extra.exclusion_reason ?? null,
  };
}

describe("getInvestorPortfolioMomentum", () => {
  it("returns null when Supabase is not configured", async () => {
    mockedCreate.mockReturnValue(null);
    await expect(getInvestorPortfolioMomentum("acme")).resolves.toBeNull();
  });

  it("returns null when the investor is not found", async () => {
    useClient(byTable({ investorsError: { message: "no rows", code: "PGRST116" } }));
    await expect(getInvestorPortfolioMomentum("ghost")).resolves.toBeNull();
  });

  it("unions both link paths, dedupes by slug, and counts heating-up companies", async () => {
    useClient(
      byTable({
        company_investors: [
          { companies: co("hot", 0.9, { momentum_why: ["news +180%"] }) },
          { companies: co("warm", 0.7) },
          { companies: co("flat", 0.5) }, // scored but below threshold
        ],
        funding_round_investors: [
          // "hot" again via the round path → deduped (counted once).
          { funding_rounds: { companies: co("hot", 0.9) } },
          // a round-only company that's also hot.
          { funding_rounds: { companies: co("rocket", 0.8) } },
        ],
      }),
    );

    const m = await getInvestorPortfolioMomentum("acme");
    expect(m).not.toBeNull();
    expect(m!.scoredCount).toBe(4); // hot, warm, flat, rocket (hot deduped)
    expect(m!.heatingUpCount).toBe(3); // hot(0.9), rocket(0.8), warm(0.7) ≥ 0.65
    expect(m!.topHeatingUp.map((c) => c.slug)).toEqual(["hot", "rocket", "warm"]);
    expect(m!.topHeatingUp[0].momentumWhy).toEqual(["news +180%"]);
    expect(m!.meanMomentum).toBeCloseTo((0.9 + 0.7 + 0.5 + 0.8) / 4, 5);
  });

  it("drops excluded companies and skips unscored ones", async () => {
    useClient(
      byTable({
        company_investors: [
          { companies: co("hot", 0.9) },
          { companies: co("junk", 0.95, { exclusion_reason: "not_a_startup" }) }, // dropped
          { companies: co("unscored", null) }, // skipped (no score)
        ],
      }),
    );
    const m = await getInvestorPortfolioMomentum("acme");
    expect(m!.scoredCount).toBe(1);
    expect(m!.heatingUpCount).toBe(1);
    expect(m!.topHeatingUp.map((c) => c.slug)).toEqual(["hot"]);
  });

  it("returns a zero aggregate (not null) when the portfolio has no scored rows", async () => {
    useClient(byTable({ company_investors: [{ companies: co("unscored", null) }] }));
    const m = await getInvestorPortfolioMomentum("acme");
    expect(m).toEqual({
      scoredCount: 0,
      heatingUpCount: 0,
      meanMomentum: null,
      topHeatingUp: [],
    });
  });
});
