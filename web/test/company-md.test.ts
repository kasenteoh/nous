// Tests for the /c/[slug].md markdown renderer (lib/company-md): per-fact
// sources inline, verified-fact annotation, omit-when-unknown, the
// computeTotalRaised invariant, and the competitor meta-leak guard.

import { describe, expect, it } from "vitest";
import { renderCompanyMarkdown } from "@/lib/company-md";
import type {
  CompanyDetail,
  CompanyRow,
  CompetitorWithResolved,
  FundingRoundWithInvestors,
} from "@/lib/types";

const ORIGIN = "https://nous.example";

function company(overrides: Partial<CompanyRow> = {}): CompanyRow {
  return {
    id: "c-1",
    name: "Acme",
    slug: "acme",
    normalized_name: "acme",
    description_short: "Acme builds robots.",
    description_long: "Longer prose about Acme.",
    primary_category: null,
    tags: ["robotics"],
    website: "https://acme.com",
    logo_url: null,
    hq_city: "Austin",
    hq_state: "TX",
    hq_country: "US",
    year_incorporated: 2021,
    industry_group: "Robotics",
    employee_count_min: 10,
    employee_count_max: 50,
    employee_count_source: null,
    last_enriched_at: null,
    discovered_via: "news",
    status: "active",
    status_source_url: null,
    consecutive_scrape_failures: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function round(
  overrides: Partial<FundingRoundWithInvestors> = {},
): FundingRoundWithInvestors {
  return {
    id: "r-1",
    company_id: "c-1",
    round_type: "Series A",
    amount_raised: 40_000_000,
    valuation_post_money: null,
    valuation_source: null,
    announced_date: "2026-03-01",
    primary_news_url: "https://techcrunch.com/acme-a",
    extraction_confidence: "high",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    leadInvestors: ["Lead Fund"],
    otherInvestors: [],
    ...overrides,
  };
}

function detail(overrides: Partial<CompanyDetail> = {}): CompanyDetail {
  return {
    company: company(),
    people: [],
    fundingRounds: [],
    competitors: [],
    investors: [],
    news: [],
    verifications: [],
    ...overrides,
  };
}

describe("renderCompanyMarkdown", () => {
  it("renders key facts with inline sources and omits unknowns", () => {
    const md = renderCompanyMarkdown(detail(), ORIGIN);
    expect(md).toContain("# Acme");
    expect(md).toContain("> Acme builds robots.");
    expect(md).toContain("- Website: https://acme.com");
    expect(md).toContain("- Headquarters: Austin, TX, US");
    expect(md).not.toContain("Status:"); // active → omitted
    expect(md).not.toContain("Total raised"); // nothing known
    expect(md).not.toContain("## Funding rounds"); // empty section hidden
    expect(md).toContain(`${ORIGIN}/c/acme`);
    expect(md).toContain(`${ORIGIN}/llms.txt`);
  });

  it("renders rounds with sources and a verified annotation when grounded", () => {
    const md = renderCompanyMarkdown(
      detail({
        fundingRounds: [round()],
        verifications: [
          {
            fact_kind: "funding_round",
            fact_ref: "r-1",
            source_url: "https://techcrunch.com/acme-a",
            claim: "Acme raised $40.0M in its Series A round.",
            supporting_quote: "raised $40 million",
          },
        ],
      }),
      ORIGIN,
    );
    expect(md).toContain("**Series A** — $40M");
    expect(md).toContain("investors: Lead Fund (lead)");
    expect(md).toContain("source: https://techcrunch.com/acme-a");
    expect(md).toContain("✓ verified against the cited source");
  });

  it("does not mark a round verified when the claim drifted", () => {
    const md = renderCompanyMarkdown(
      detail({
        fundingRounds: [round({ amount_raised: 45_000_000 })],
        verifications: [
          {
            fact_kind: "funding_round",
            fact_ref: "r-1",
            source_url: "https://techcrunch.com/acme-a",
            claim: "Acme raised $40.0M in its Series A round.", // stale
            supporting_quote: "raised $40 million",
          },
        ],
      }),
      ORIGIN,
    );
    expect(md).not.toContain("✓ verified");
  });

  it("uses the shared total-raised invariant (stated figure cites its article)", () => {
    const md = renderCompanyMarkdown(
      detail({
        company: company({
          total_raised_usd: 100_000_000,
          total_raised_source_url: "https://techcrunch.com/acme-total",
          total_raised_as_of: "2026-05-01",
        }),
        fundingRounds: [round()],
      }),
      ORIGIN,
    );
    expect(md).toContain("- Total raised: $100M");
    expect(md).toContain("source: https://techcrunch.com/acme-total");
  });

  it("drops competitor rows that leak model scratch notes", () => {
    const clean: CompetitorWithResolved = {
      id: "comp-1",
      company_id: "c-1",
      competitor_company_id: null,
      competitor_name: "Real Rival",
      description: null,
      reasoning: null,
      rank: 1,
      source: "llm_inferred",
      source_url: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      resolved: null,
    };
    const leaked: CompetitorWithResolved = {
      ...clean,
      id: "comp-2",
      competitor_name: "Leaky",
      reasoning: "Included temporarily for evaluation but should be dropped.",
    };
    const md = renderCompanyMarkdown(
      detail({ competitors: [clean, leaked] }),
      ORIGIN,
    );
    expect(md).toContain("- Real Rival");
    expect(md).not.toContain("Leaky");
    expect(md).toContain("## Competitors (AI-inferred)");
  });

  it("omits the blockquote lead for a describe-fallback description", () => {
    // A third-party-grounded one-liner (description_source === "fallback",
    // migration 0045) must not lead this machine-consumed .md surface, which has
    // no per-fact attribution slot.
    const md = renderCompanyMarkdown(
      detail({
        company: company({
          description_short: "Acme builds robots.",
          description_source: "fallback",
        }),
      }),
      ORIGIN,
    );
    expect(md).toContain("# Acme");
    expect(md).not.toContain("> Acme builds robots.");
  });

  it("keeps the blockquote lead for an own-website description (absent source)", () => {
    const c = company();
    // Byte-identical to today: the field is absent (pre-migration prod row).
    expect("description_source" in c).toBe(false);
    const md = renderCompanyMarkdown(detail({ company: c }), ORIGIN);
    expect(md).toContain("> Acme builds robots.");
  });

  it("annotates a non-active status with its source", () => {
    const md = renderCompanyMarkdown(
      detail({
        company: company({
          status: "acquired",
          status_source_url: "https://reuters.com/acme-acquired",
        }),
      }),
      ORIGIN,
    );
    expect(md).toContain(
      "- Status: acquired — source: https://reuters.com/acme-acquired",
    );
  });
});
