// Page-level smoke tests for /trending ("Heating up"): the empty state when no
// company is scored yet (the pre-migration [] path), and the populated grid +
// "Momentum as of" rider. Data layer mocked, same pattern as
// companies-page-semantic.test.tsx (render `await Page()`).

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TrendingPage from "@/app/trending/page";
import { listHeatingUpCompanies } from "@/lib/queries";
import type { MomentumCompany } from "@/lib/types";

vi.mock("@/lib/queries", () => ({
  listHeatingUpCompanies: vi.fn(),
}));

const mockedList = vi.mocked(listHeatingUpCompanies);

function momentumCompany(
  overrides: Partial<MomentumCompany> = {},
): MomentumCompany {
  return {
    slug: "acme",
    name: "Acme",
    hq_city: "San Francisco",
    hq_state: "CA",
    industry_group: "Fintech",
    description_short: "Payments infra.",
    status: "active",
    logo_url: null,
    momentumScore: 0.82,
    momentumComputedAt: "2026-07-13T06:00:00Z",
    momentumWhy: ["+40% team", "5 news mentions"],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("/trending page", () => {
  it("shows the empty state (and no cards) when nothing is scored yet", async () => {
    mockedList.mockResolvedValue([]);
    render(await TrendingPage());

    expect(
      screen.getByText(
        "No momentum scores yet — check back once the signal has warmed up.",
      ),
    ).toBeInTheDocument();
    // The empty state points at /new.
    expect(
      screen.getByRole("link", { name: /new this week/i }),
    ).toHaveAttribute("href", "/new");
    // No company links rendered.
    expect(
      screen.queryByRole("link", { name: "Acme" }),
    ).not.toBeInTheDocument();
    // No "Momentum as of" rider without a scored row.
    expect(screen.queryByText(/Momentum as of/)).not.toBeInTheDocument();
  });

  it("renders a card per company + the 'Momentum as of' rider when populated", async () => {
    mockedList.mockResolvedValue([
      momentumCompany({ slug: "acme", name: "Acme" }),
      momentumCompany({
        slug: "beta",
        name: "Beta",
        momentumScore: 0.71,
        momentumWhy: ["3 news mentions"],
      }),
    ]);
    render(await TrendingPage());

    expect(
      screen.getByRole("heading", { name: "Heating up", level: 1 }),
    ).toBeInTheDocument();
    // Each card is a single <Link> wrapping the whole body, so match on the
    // company name within the link's (longer) accessible name, then its href.
    expect(screen.getByRole("heading", { name: "Acme", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Beta", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Acme/ })).toHaveAttribute(
      "href",
      "/c/acme",
    );
    expect(screen.getByRole("link", { name: /Beta/ })).toHaveAttribute(
      "href",
      "/c/beta",
    );
    // Both are above the badge threshold → both light the pill.
    expect(screen.getAllByText("🔥 Heating up")).toHaveLength(2);
    // The "why" line renders the joined breakdown.
    expect(screen.getByText("+40% team · 5 news mentions")).toBeInTheDocument();
    // The rider derives from the top row's momentumComputedAt (formatDate).
    expect(screen.getByText(/Momentum as of July 13, 2026\./)).toBeInTheDocument();
  });
});
