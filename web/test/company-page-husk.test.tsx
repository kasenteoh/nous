import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CompanyPage from "@/app/c/[slug]/page";
import {
  getAlsoBackedBy,
  getCompanyBySlug,
  getInvestorNameToSlugMap,
  getRelatedCompanies,
} from "@/lib/queries";
import type { CompanyDetail, CompanyRow } from "@/lib/types";

// The page is an async server component; its data layer is mocked so the test
// can drive the husk/placeholder state directly. Rendering `await Page(props)`
// works because the component uses no server-only APIs beyond these queries.
vi.mock("@/lib/queries", () => ({
  getAlsoBackedBy: vi.fn(),
  getCompanyBySlug: vi.fn(),
  getInvestorNameToSlugMap: vi.fn(),
  getRelatedCompanies: vi.fn(),
}));

function huskCompany(overrides: Partial<CompanyRow> = {}): CompanyRow {
  return {
    id: "c-husk",
    name: "Acme Robotics",
    slug: "acme-robotics",
    normalized_name: "acme robotics",
    description_short: null,
    description_long: null,
    primary_category: null,
    tags: null,
    website: null,
    logo_url: null,
    hq_city: null,
    hq_state: null,
    hq_country: null,
    year_incorporated: null,
    industry_group: null,
    employee_count_min: null,
    employee_count_max: null,
    employee_count_source: null,
    last_enriched_at: null,
    discovered_via: "vc_portfolio",
    status: "active",
    status_source_url: null,
    consecutive_scrape_failures: 0,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function detail(overrides: Partial<CompanyDetail> = {}): CompanyDetail {
  return {
    company: huskCompany(),
    people: [],
    fundingRounds: [],
    competitors: [],
    investors: [],
    news: [],
    ...overrides,
  };
}

async function renderCompanyPage(): Promise<void> {
  render(
    await CompanyPage({
      params: Promise.resolve({ slug: "acme-robotics" }),
    }),
  );
}

beforeEach(() => {
  vi.mocked(getInvestorNameToSlugMap).mockResolvedValue({});
  vi.mocked(getRelatedCompanies).mockResolvedValue([]);
  vi.mocked(getAlsoBackedBy).mockResolvedValue([]);
});

describe("company page husk placeholder", () => {
  it("shows the not-yet-profiled box for a true husk (no description, rounds, news, competitors, or investors)", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(detail());
    await renderCompanyPage();

    expect(
      screen.getByText(/built a full profile yet/),
    ).toBeInTheDocument();
    // The box names the company and its discovery source.
    expect(screen.getByText("Acme Robotics", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText(/via VC portfolio but/)).toBeInTheDocument();
  });

  it("does NOT claim 'no profile yet' when the company has funding history", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({
        fundingRounds: [
          {
            id: "r1",
            company_id: "c-husk",
            round_type: "Series A",
            amount_raised: 10_000_000,
            valuation_post_money: null,
            valuation_source: null,
            announced_date: "2026-01-15",
            primary_news_url: null,
            extraction_confidence: null,
            created_at: "2026-01-16T00:00:00Z",
            updated_at: "2026-01-16T00:00:00Z",
            leadInvestors: [],
            otherInvestors: [],
          },
        ],
      }),
    );
    await renderCompanyPage();

    expect(
      screen.queryByText(/built a full profile yet/),
    ).not.toBeInTheDocument();
    // The funding section renders instead.
    expect(
      screen.getByRole("heading", { name: "Funding History" }),
    ).toBeInTheDocument();
  });

  it("does NOT show the husk box once a description exists", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({
        company: huskCompany({ description_short: "Builds robots." }),
      }),
    );
    await renderCompanyPage();

    expect(
      screen.queryByText(/built a full profile yet/),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Builds robots.")).toBeInTheDocument();
  });
});
