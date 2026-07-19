import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CompanyPage, { generateMetadata } from "@/app/c/[slug]/page";
import {
  getAlsoBackedBy,
  getCareerMoves,
  getCompanyBySlug,
  getInvestorNameToSlugMap,
  getRelatedCompanies,
  getSimilarCompanies,
} from "@/lib/queries";
import type { CompanyDetail, CompanyRow } from "@/lib/types";

// The page is an async server component; its data layer is mocked so the test
// can drive the husk/placeholder state directly. Rendering `await Page(props)`
// works because the component uses no server-only APIs beyond these queries.
vi.mock("@/lib/queries", () => ({
  getAlsoBackedBy: vi.fn(),
  getCareerMoves: vi.fn(),
  getCompanyBySlug: vi.fn(),
  getInvestorNameToSlugMap: vi.fn(),
  getRelatedCompanies: vi.fn(),
  getSimilarCompanies: vi.fn(),
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
    verifications: [],
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
  vi.mocked(getSimilarCompanies).mockResolvedValue([]);
  vi.mocked(getAlsoBackedBy).mockResolvedValue([]);
  vi.mocked(getCareerMoves).mockResolvedValue([]);
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
    // The funding rail renders instead.
    expect(
      screen.getByRole("heading", { name: "Funding" }),
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

// ─── Funding / news split (2026-07-18 design) ────────────────────────────────
// The page calls buildTimeline once, splits by kind, and owns the both-empty
// line; each section omits itself when empty.

describe("company page funding/news split", () => {
  const seriesA = {
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
  };
  // Far outside the Series A's ±14d window → a standalone story.
  const standaloneStory = {
    id: "n1",
    url: "https://reuters.com/ipo-chatter",
    title: "Acme mulls IPO",
    source: "reuters.com",
    published_date: "2026-06-01",
    funding_round_id: null,
  };

  it("renders Funding then In the news, in that order", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({ fundingRounds: [seriesA], news: [standaloneStory] }),
    );
    await renderCompanyPage();

    const headings = screen
      .getAllByRole("heading")
      .map((h) => h.textContent ?? "");
    expect(headings.indexOf("Funding")).toBeGreaterThanOrEqual(0);
    expect(headings.indexOf("In the news")).toBeGreaterThan(
      headings.indexOf("Funding"),
    );
    expect(
      screen.queryByText("No funding rounds or news recorded yet."),
    ).not.toBeInTheDocument();
  });

  it("omits In the news when every article is round coverage or absent", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({ fundingRounds: [seriesA] }),
    );
    await renderCompanyPage();

    expect(
      screen.getByRole("heading", { name: "Funding" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "In the news" }),
    ).not.toBeInTheDocument();
  });

  it("omits Funding and renders news alone when there are no rounds", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({ news: [standaloneStory] }),
    );
    await renderCompanyPage();

    expect(
      screen.queryByRole("heading", { name: "Funding" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "In the news" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Acme mulls IPO" }),
    ).toBeInTheDocument();
  });

  it("shows the single both-empty line when there are no rounds and no news", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(detail());
    await renderCompanyPage();

    expect(
      screen.getByText("No funding rounds or news recorded yet."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Funding" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "In the news" }),
    ).not.toBeInTheDocument();
  });
});

// ─── describe-fallback gating (migration 0045) ───────────────────────────────
// A third-party-grounded description_short (description_source === "fallback")
// stays VISIBLE on-site with an attribution rider, but must never reach a
// machine-syndicated surface with no attribution: page <meta>, the Organization
// / FAQ JSON-LD. undefined/null source → own-website → byte-identical to today.

describe("company page describe-fallback visible attribution", () => {
  it("shows the fallback tagline with its attribution rider", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({
        company: huskCompany({
          description_short: "Builds humanoid robots.",
          description_source: "fallback",
        }),
      }),
    );
    await renderCompanyPage();

    expect(screen.getByText("Builds humanoid robots.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Description written by nous from Wikidata and press coverage",
      ),
    ).toBeInTheDocument();
  });

  it("omits the attribution rider for an own-website description", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({
        // No description_source → own-website.
        company: huskCompany({ description_short: "Builds humanoid robots." }),
      }),
    );
    await renderCompanyPage();

    expect(screen.getByText("Builds humanoid robots.")).toBeInTheDocument();
    expect(
      screen.queryByText(
        "Description written by nous from Wikidata and press coverage",
      ),
    ).not.toBeInTheDocument();
  });
});

describe("company page metadata gating", () => {
  async function metaDescription(company: CompanyRow): Promise<string> {
    vi.mocked(getCompanyBySlug).mockResolvedValue(detail({ company }));
    const meta = await generateMetadata({
      params: Promise.resolve({ slug: "acme-robotics" }),
    });
    return meta.description as string;
  }

  it("uses the location/industry fallback for a describe-fallback description", async () => {
    const desc = await metaDescription(
      huskCompany({
        description_short: "Builds humanoid robots.",
        description_source: "fallback",
        industry_group: "Robotics",
        hq_city: "Austin",
        hq_state: "TX",
      }),
    );
    expect(desc).not.toContain("Builds humanoid robots.");
    expect(desc).toContain("Robotics");
    expect(desc).toContain("Acme Robotics");
  });

  it("uses an own-website description verbatim in the meta description", async () => {
    const desc = await metaDescription(
      huskCompany({ description_short: "Builds humanoid robots." }),
    );
    expect(desc).toBe("Builds humanoid robots.");
  });

  it("treats an absent description_source as own-website (byte-identical)", async () => {
    const company = huskCompany({ description_short: "Builds humanoid robots." });
    // The field is genuinely absent (pre-migration prod row), not just null.
    expect("description_source" in company).toBe(false);
    const desc = await metaDescription(company);
    expect(desc).toBe("Builds humanoid robots.");
  });
});

describe("company page structured-data gating", () => {
  function jsonLdBlocks(container: HTMLElement): Record<string, unknown>[] {
    return Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    ).map((el) => JSON.parse(el.textContent ?? "{}") as Record<string, unknown>);
  }

  it("drops the description from Organization + FAQ JSON-LD for a fallback row", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({
        company: huskCompany({
          description_short: "Builds humanoid robots.",
          description_source: "fallback",
          hq_city: "Austin",
          hq_state: "TX",
        }),
      }),
    );
    const { container } = render(
      await CompanyPage({ params: Promise.resolve({ slug: "acme-robotics" }) }),
    );
    const blocks = jsonLdBlocks(container);

    const org = blocks.find((b) => b["@type"] === "Organization");
    expect(org).toBeDefined();
    expect("description" in org!).toBe(false);

    // The FAQ block still renders (the location question is answerable) but must
    // not carry the "What does X do?" Q&A sourced from the fallback description.
    const faq = blocks.find((b) => b["@type"] === "FAQPage");
    expect(faq).toBeDefined();
    const questions = (faq!.mainEntity as { name: string }[]).map((q) => q.name);
    expect(questions).not.toContain("What does Acme Robotics do?");
    expect(questions).toContain("Where is Acme Robotics based?");
  });

  it("keeps the description in Organization + FAQ JSON-LD for an own-website row", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({
        company: huskCompany({
          description_short: "Builds humanoid robots.",
          hq_city: "Austin",
          hq_state: "TX",
        }),
      }),
    );
    const { container } = render(
      await CompanyPage({ params: Promise.resolve({ slug: "acme-robotics" }) }),
    );
    const blocks = jsonLdBlocks(container);

    const org = blocks.find((b) => b["@type"] === "Organization");
    expect(org!.description).toBe("Builds humanoid robots.");

    const faq = blocks.find((b) => b["@type"] === "FAQPage");
    const questions = (faq!.mainEntity as { name: string }[]).map((q) => q.name);
    expect(questions).toContain("What does Acme Robotics do?");
  });
});

describe("company page provenance wiring", () => {
  it("cites website provenance as a Sources row with its source-type label", async () => {
    // Regression: the page must ADD website_source_url to the citations list
    // (like total-raised/status), otherwise the "Website / Wikidata / VC
    // portfolio" source-type labels are unreachable on the real page even though
    // citationSourceType handles them in isolation.
    vi.mocked(getCompanyBySlug).mockResolvedValue(
      detail({
        company: huskCompany({
          description_short: "Builds robots.", // non-husk → full page renders
          website: "https://acme.com",
          website_source: "wikidata",
          website_source_url: "https://www.wikidata.org/wiki/Q42",
        }),
      }),
    );
    await renderCompanyPage();

    const section = screen
      .getByRole("heading", { name: "Sources" })
      .closest("section") as HTMLElement;
    expect(section).not.toBeNull();
    // The website provenance is listed and tagged by its source type.
    expect(within(section).getByText("Website")).toBeInTheDocument();
    expect(within(section).getByText("· Wikidata")).toBeInTheDocument();
  });
});
