// Page-level tests for /companies semantic-search wiring (E-2): when the page
// embeds the query, what it passes to listCompaniesHybrid, the "includes
// semantic matches" disclosure, and the husk fallback staying keyed on the
// LEXICAL total. Data layer + embedder are mocked, same pattern as
// company-page-husk.test.tsx (render `await Page(props)`).

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CompaniesPage from "@/app/companies/page";
import { embedQuery } from "@/lib/embed-query";
import {
  listCompaniesHybrid,
  listDiscoveredViaValues,
  listIndustryGroups,
  searchHuskFallback,
  type CompanyListSort,
} from "@/lib/queries";
import type { CompanyListRow } from "@/lib/types";

vi.mock("@/lib/queries", () => ({
  listCompanies: vi.fn(),
  listCompaniesHybrid: vi.fn(),
  listIndustryGroups: vi.fn(),
  listDiscoveredViaValues: vi.fn(),
  searchHuskFallback: vi.fn(),
}));

vi.mock("@/lib/embed-query", () => ({
  embedQuery: vi.fn(),
}));

const mockedHybrid = vi.mocked(listCompaniesHybrid);
const mockedEmbed = vi.mocked(embedQuery);

function row(slug: string): CompanyListRow {
  return {
    slug,
    name: `Co ${slug}`,
    hq_city: null,
    hq_state: null,
    industry_group: null,
    description_short: `About ${slug}.`,
    status: "active",
    logo_url: null,
  };
}

function hybridResult(
  rows: CompanyListRow[],
  overrides: Partial<Awaited<ReturnType<typeof listCompaniesHybrid>>> = {},
) {
  return {
    rows,
    total: rows.length,
    semanticCount: 0,
    lexicalTotal: rows.length,
    ...overrides,
  };
}

async function renderPage(params: Record<string, string>): Promise<void> {
  render(await CompaniesPage({ searchParams: Promise.resolve(params) }));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listIndustryGroups).mockResolvedValue([]);
  vi.mocked(listDiscoveredViaValues).mockResolvedValue([]);
  vi.mocked(searchHuskFallback).mockResolvedValue([]);
  mockedEmbed.mockResolvedValue([0.1, 0.2]);
  mockedHybrid.mockResolvedValue(hybridResult([row("alpha")]));
});

describe("/companies semantic wiring", () => {
  it("embeds q and passes the vector + default sort to the hybrid query", async () => {
    await renderPage({ q: "ai for logistics" });

    expect(mockedEmbed).toHaveBeenCalledWith("ai for logistics");
    expect(mockedHybrid).toHaveBeenCalledWith(
      expect.objectContaining({
        search: "ai for logistics",
        // undefined (not "name_asc") — the blend gate's default-order signal.
        sort: undefined,
        offset: 0,
      }),
      [0.1, 0.2],
    );
  });

  it("skips the embedder under an explicit sort and passes the sort through", async () => {
    await renderPage({ q: "ai for logistics", sort: "funding_desc" });

    expect(mockedEmbed).not.toHaveBeenCalled();
    expect(mockedHybrid).toHaveBeenCalledWith(
      expect.objectContaining({ sort: "funding_desc" satisfies CompanyListSort }),
      null,
    );
  });

  it("skips the embedder beyond page 1 and under column filters", async () => {
    await renderPage({ q: "ai", page: "2" });
    expect(mockedEmbed).not.toHaveBeenCalled();

    await renderPage({ q: "ai", industry: "Fintech" });
    expect(mockedEmbed).not.toHaveBeenCalled();
  });

  it("skips the embedder when q is absent", async () => {
    await renderPage({});
    expect(mockedEmbed).not.toHaveBeenCalled();
  });

  it("discloses appended semantic matches next to the result count", async () => {
    mockedHybrid.mockResolvedValue(
      hybridResult([row("alpha"), row("gamma")], {
        total: 2,
        semanticCount: 1,
        lexicalTotal: 1,
      }),
    );

    await renderPage({ q: "ai for logistics" });

    expect(screen.getByText(/includes semantic matches/)).toBeInTheDocument();
    expect(screen.getByText(/Showing 1–2 of 2/)).toBeInTheDocument();
  });

  it("shows no semantic note on a pure lexical result", async () => {
    await renderPage({ q: "ai for logistics" });

    expect(screen.queryByText(/includes semantic matches/)).toBeNull();
  });

  it("keeps the husk fallback keyed on the lexical total, not blended rows", async () => {
    // Semantic neighbors landed, but nothing matched "anthropic" lexically —
    // the "we track these" box must still get its chance.
    mockedHybrid.mockResolvedValue(
      hybridResult([row("some-ai-co")], {
        total: 1,
        semanticCount: 1,
        lexicalTotal: 0,
      }),
    );
    vi.mocked(searchHuskFallback).mockResolvedValue([
      { slug: "anthropic", name: "Anthropic" },
    ]);

    await renderPage({ q: "anthropic" });

    expect(vi.mocked(searchHuskFallback)).toHaveBeenCalledWith("anthropic");
    expect(screen.getByText("Anthropic")).toBeInTheDocument();
  });
});
