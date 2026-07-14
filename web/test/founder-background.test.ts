import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { createSupabaseServerClient } from "@/lib/db";
import { getCareerMoves } from "@/lib/queries";
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

// A career_moves join row as PostgREST returns it (prior_company embedded).
const resolvedRow = {
  person_name: "Rodrigo Liang",
  prior_company_name: "Oracle",
  prior_role: "SVP of engineering",
  start_year: null,
  end_year: null,
  prior_company: { slug: "oracle", name: "Oracle", exclusion_reason: null },
};

const unlinkedRow = {
  person_name: "Rodrigo Liang",
  prior_company_name: "Sun Microsystems",
  prior_role: null,
  start_year: 1999,
  end_year: 2011,
  prior_company: null, // not in catalog
};

const excludedRow = {
  person_name: "Kunle Olukotun",
  prior_company_name: "Afara Websystems",
  prior_role: null,
  start_year: null,
  end_year: null,
  prior_company: { slug: "afara", name: "Afara", exclusion_reason: "not_a_startup" },
};

describe("getCareerMoves", () => {
  it("returns [] when Supabase is not configured", async () => {
    mockedCreate.mockReturnValue(null);
    await expect(getCareerMoves("co-1")).resolves.toEqual([]);
  });

  it("returns [] on a query error (migration-order-free degradation)", async () => {
    useClient(() => ({ error: { message: "relation career_moves does not exist" } }));
    await expect(getCareerMoves("co-1")).resolves.toEqual([]);
  });

  it("queries career_moves with the FK-hint embed, filtered + ordered", async () => {
    const mock = useClient(() => ({ data: [resolvedRow] }));
    await getCareerMoves("co-1");

    const b = mock.buildersFor("career_moves")[0];
    expect(b.table).toBe("career_moves");
    // The FK hint is REQUIRED — career_moves has two FKs to companies.
    expect(
      b.calls.some(
        (c) =>
          c.method === "select" &&
          typeof c.args[0] === "string" &&
          (c.args[0] as string).includes("companies!prior_company_id"),
      ),
    ).toBe(true);
    expect(b.has("eq", "company_id", "co-1")).toBe(true);
    expect(b.has("order", "person_normalized_name", { ascending: true })).toBe(true);
  });

  it("maps rows: links resolved companies, keeps unresolved/excluded as text", async () => {
    useClient(() => ({ data: [resolvedRow, unlinkedRow, excludedRow] }));
    const rows = await getCareerMoves("co-1");

    expect(rows).toEqual([
      {
        personName: "Rodrigo Liang",
        priorCompanyName: "Oracle",
        priorRole: "SVP of engineering",
        startYear: null,
        endYear: null,
        priorCompanySlug: "oracle", // resolved + shown → linked
      },
      {
        personName: "Rodrigo Liang",
        priorCompanyName: "Sun Microsystems",
        priorRole: null,
        startYear: 1999,
        endYear: 2011,
        priorCompanySlug: null, // not in catalog → text only
      },
      {
        personName: "Kunle Olukotun",
        priorCompanyName: "Afara Websystems",
        priorRole: null,
        startYear: null,
        endYear: null,
        priorCompanySlug: null, // excluded → drop the link, keep the name
      },
    ]);
  });

  it("handles the embed arriving as a single-element array", async () => {
    useClient(() => ({
      data: [{ ...resolvedRow, prior_company: [resolvedRow.prior_company] }],
    }));
    const rows = await getCareerMoves("co-1");
    expect(rows[0].priorCompanySlug).toBe("oracle");
  });
});
