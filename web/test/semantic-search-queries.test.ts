// Query-layer tests for semantic search (E-2): semanticCompanySearch (the
// .rpc() wrapper over migration 0035's semantic_companies function) and
// listCompaniesHybrid (the lexical-first blend). Same observable boundary as
// queries.test.ts / themes-queries.test.ts: which calls were made against the
// mock builder, and what the code does with the scripted response.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSupabaseServerClient } from "@/lib/db";
import { listCompaniesHybrid, semanticCompanySearch } from "@/lib/queries";
import {
  createMockSupabase,
  type MockSupabase,
  type QueryResult,
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

/** A lexical row as listCompanies' select returns it. */
function lexRow(slug: string) {
  return {
    slug,
    name: `Lex ${slug}`,
    hq_city: null,
    hq_state: null,
    industry_group: "DevTools",
    description_short: `Lexical match ${slug}.`,
    status: "active",
    logo_url: null,
  };
}

/** A row as the semantic_companies() RPC returns it (card projection + similarity). */
function rpcRow(slug: string, similarity = 0.8, overrides: Record<string, unknown> = {}) {
  return {
    slug,
    name: `Sem ${slug}`,
    hq_city: "Austin",
    hq_state: "TX",
    industry_group: "AI",
    description_short: `Semantic neighbor ${slug}.`,
    status: "active",
    logo_url: null,
    similarity,
    ...overrides,
  };
}

/**
 * Responder for the hybrid flow: `companies` table queries get the lexical
 * page, the semantic_companies RPC gets `rpcRows`.
 */
function hybridResponder(
  lexRows: ReturnType<typeof lexRow>[],
  lexTotal: number,
  rpcRows: unknown[],
): Responder {
  return (builder): QueryResult => {
    if (builder.table === "rpc:semantic_companies") return { data: rpcRows };
    if (builder.table === "companies") {
      return { data: lexRows, count: lexTotal };
    }
    throw new Error(`unexpected table ${builder.table}`);
  };
}

const EMBEDDING = [0.25, -0.5, 0.125];

// ─── semanticCompanySearch ────────────────────────────────────────────────────

describe("semanticCompanySearch", () => {
  it("calls the RPC with the pgvector literal and maps the card projection", async () => {
    const mock = useClient(() => ({ data: [rpcRow("acme", 0.91)] }));

    const rows = await semanticCompanySearch(EMBEDDING);

    const rpc = mock.buildersFor("rpc:semantic_companies")[0];
    expect(
      rpc.has("rpc", "semantic_companies", {
        query_embedding: "[0.25,-0.5,0.125]",
        match_count: 30,
      }),
    ).toBe(true);
    expect(rows).toEqual([
      {
        slug: "acme",
        name: "Sem acme",
        hq_city: "Austin",
        hq_state: "TX",
        industry_group: "AI",
        description_short: "Semantic neighbor acme.",
        status: "active",
        logo_url: null,
      },
    ]);
  });

  it("drops rows without slug or name (defense in depth)", async () => {
    useClient(() => ({
      data: [
        rpcRow("ok"),
        rpcRow("no-name", 0.7, { name: null }),
        rpcRow("", 0.7, { slug: null }),
      ],
    }));

    const rows = await semanticCompanySearch(EMBEDDING);
    expect(rows.map((r) => r.slug)).toEqual(["ok"]);
  });

  it("returns [] on an RPC error", async () => {
    useClient(() => ({
      data: null,
      error: { message: "function semantic_companies does not exist" },
    }));

    expect(await semanticCompanySearch(EMBEDDING)).toEqual([]);
  });

  it("returns [] when Supabase is unconfigured", async () => {
    mockedCreate.mockImplementation(() => {
      throw new Error("Missing SUPABASE_URL");
    });

    expect(await semanticCompanySearch(EMBEDDING)).toEqual([]);
  });
});

// ─── listCompaniesHybrid ──────────────────────────────────────────────────────

describe("listCompaniesHybrid", () => {
  it("appends deduped semantic extras after the lexical rows", async () => {
    const mock = useClient(
      hybridResponder(
        [lexRow("alpha"), lexRow("beta")],
        2,
        // "alpha" duplicates a lexical slug and must be dropped; the rest
        // append in RPC (cosine) order.
        [rpcRow("alpha"), rpcRow("gamma"), rpcRow("delta")],
      ),
    );

    const result = await listCompaniesHybrid(
      { search: "vector databases", limit: 30, offset: 0 },
      EMBEDDING,
    );

    // Lexical-first ordering: exact/substring intent stays on top.
    expect(result.rows.map((r) => r.slug)).toEqual([
      "alpha",
      "beta",
      "gamma",
      "delta",
    ]);
    expect(result.semanticCount).toBe(2);
    expect(result.lexicalTotal).toBe(2);
    // The blended total counts the appended rows so "Showing X–Y of N" is honest.
    expect(result.total).toBe(4);
    expect(mock.buildersFor("rpc:semantic_companies")).toHaveLength(1);
  });

  it("skips the RPC entirely when the embedder returned null", async () => {
    const mock = useClient(hybridResponder([lexRow("alpha")], 1, []));

    const result = await listCompaniesHybrid(
      { search: "vector databases", limit: 30, offset: 0 },
      null,
    );

    expect(mock.buildersFor("rpc:semantic_companies")).toHaveLength(0);
    expect(result.rows.map((r) => r.slug)).toEqual(["alpha"]);
    expect(result.semanticCount).toBe(0);
    expect(result.lexicalTotal).toBe(1);
  });

  it("bypasses blending under an explicit sort", async () => {
    const mock = useClient(hybridResponder([lexRow("alpha")], 1, []));

    const result = await listCompaniesHybrid(
      { search: "vector databases", sort: "funding_desc", limit: 30, offset: 0 },
      EMBEDDING,
    );

    expect(mock.buildersFor("rpc:semantic_companies")).toHaveLength(0);
    expect(result.semanticCount).toBe(0);
  });

  it("bypasses blending beyond page 1", async () => {
    const mock = useClient(hybridResponder([lexRow("page2")], 31, []));

    const result = await listCompaniesHybrid(
      { search: "vector databases", limit: 30, offset: 30 },
      EMBEDDING,
    );

    expect(mock.buildersFor("rpc:semantic_companies")).toHaveLength(0);
    expect(result.semanticCount).toBe(0);
  });

  it("bypasses blending when a column filter is active", async () => {
    const mock = useClient(hybridResponder([lexRow("alpha")], 1, []));

    const result = await listCompaniesHybrid(
      {
        search: "vector databases",
        industry_group: "Fintech",
        limit: 30,
        offset: 0,
      },
      EMBEDDING,
    );

    expect(mock.buildersFor("rpc:semantic_companies")).toHaveLength(0);
    expect(result.semanticCount).toBe(0);
  });

  it("appends nothing when the lexical page is already full", async () => {
    const mock = useClient(
      hybridResponder([lexRow("a"), lexRow("b")], 5, [rpcRow("extra")]),
    );

    const result = await listCompaniesHybrid(
      { search: "vector databases", limit: 2, offset: 0 },
      EMBEDDING,
    );

    // The RPC ran concurrently, but its rows are discarded — pagination over
    // the 5 lexical matches stays pure lexical.
    expect(mock.buildersFor("rpc:semantic_companies")).toHaveLength(1);
    expect(result.rows.map((r) => r.slug)).toEqual(["a", "b"]);
    expect(result.semanticCount).toBe(0);
    expect(result.total).toBe(5);
  });

  it("caps extras to the room left on the page", async () => {
    useClient(
      hybridResponder(
        [lexRow("a")],
        1,
        [rpcRow("s1"), rpcRow("s2"), rpcRow("s3")],
      ),
    );

    const result = await listCompaniesHybrid(
      { search: "vector databases", limit: 3, offset: 0 },
      EMBEDDING,
    );

    expect(result.rows.map((r) => r.slug)).toEqual(["a", "s1", "s2"]);
    expect(result.semanticCount).toBe(2);
    expect(result.total).toBe(3);
  });

  it("reports semanticCount 0 when every neighbor was already a lexical match", async () => {
    useClient(
      hybridResponder([lexRow("alpha")], 1, [rpcRow("alpha", 0.99)]),
    );

    const result = await listCompaniesHybrid(
      { search: "alpha", limit: 30, offset: 0 },
      EMBEDDING,
    );

    expect(result.rows.map((r) => r.slug)).toEqual(["alpha"]);
    expect(result.semanticCount).toBe(0);
    expect(result.total).toBe(1);
  });
});
