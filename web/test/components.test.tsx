import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Competitors } from "@/components/Competitors";
import { FundingHistory } from "@/components/FundingHistory";
import { Investors } from "@/components/Investors";
import { Sources } from "@/components/Sources";
import { StatusBadge } from "@/components/StatusBadge";
import type {
  CompanyInvestorRow,
  CompetitorWithResolved,
  FundingRoundWithInvestors,
} from "@/lib/types";

// ─── Fixtures ─────────────────────────────────────────────────────────────────

let fixtureSeq = 0;

function competitor(
  overrides: Partial<CompetitorWithResolved> = {},
): CompetitorWithResolved {
  fixtureSeq += 1;
  return {
    id: `comp-${fixtureSeq}`,
    company_id: "c-main",
    competitor_company_id: null,
    competitor_name: `Competitor ${fixtureSeq}`,
    description: null,
    reasoning: null,
    rank: fixtureSeq,
    source: "llm_inferred",
    source_url: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    resolved: null,
    ...overrides,
  };
}

function round(
  overrides: Partial<FundingRoundWithInvestors> = {},
): FundingRoundWithInvestors {
  fixtureSeq += 1;
  return {
    id: `round-${fixtureSeq}`,
    company_id: "c-main",
    round_type: "Series A",
    amount_raised: 15_000_000,
    valuation_post_money: null,
    valuation_source: null,
    announced_date: "2026-01-15",
    primary_news_url: null,
    extraction_confidence: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    leadInvestors: [],
    otherInvestors: [],
    ...overrides,
  };
}

// ─── Competitors ──────────────────────────────────────────────────────────────

describe("Competitors", () => {
  it("drops rows whose reasoning is leaked LLM scratch-text", () => {
    render(
      <Competitors
        competitors={[
          competitor({ competitor_name: "Real Rival" }),
          competitor({
            competitor_name: "Leaked Rival",
            reasoning:
              "Included temporarily for evaluation but should be dropped.",
          }),
        ]}
      />,
    );
    expect(screen.getByText("Real Rival")).toBeInTheDocument();
    expect(screen.queryByText("Leaked Rival")).not.toBeInTheDocument();
  });

  it("drops rows whose description is leaked meta-text", () => {
    render(
      <Competitors
        competitors={[
          competitor({ competitor_name: "Kept" }),
          competitor({
            competitor_name: "Placeholder Row",
            description: "This is a placeholder entry, do not display.",
          }),
          competitor({
            competitor_name: "Non-competitor Row",
            reasoning: "Actually not a real competitor of the subject.",
          }),
        ]}
      />,
    );
    expect(screen.getByText("Kept")).toBeInTheDocument();
    expect(screen.queryByText("Placeholder Row")).not.toBeInTheDocument();
    expect(screen.queryByText("Non-competitor Row")).not.toBeInTheDocument();
  });

  it("renders nothing at all when every competitor is a leaked row", () => {
    const { container } = render(
      <Competitors
        competitors={[
          competitor({ reasoning: "placeholder" }),
          competitor({ description: "for evaluation only" }),
        ]}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("links resolved competitors to /c/[slug] and leaves unresolved ones as plain text", () => {
    render(
      <Competitors
        competitors={[
          competitor({
            competitor_name: "Rival Inc",
            resolved: { slug: "rival", name: "Rival Inc" },
          }),
          competitor({ competitor_name: "Ghost Co" }),
        ]}
      />,
    );
    expect(screen.getByRole("link", { name: "Rival Inc" })).toHaveAttribute(
      "href",
      "/c/rival",
    );
    expect(
      screen.queryByRole("link", { name: "Ghost Co" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Ghost Co")).toBeInTheDocument();
  });

  it("labels provenance: TechCrunch-grounded vs AI-inferred", () => {
    render(
      <Competitors
        competitors={[
          competitor({
            competitor_name: "Grounded",
            source: "techcrunch",
            source_url: "https://techcrunch.com/article",
          }),
          competitor({ competitor_name: "Inferred", source: "llm_inferred" }),
        ]}
      />,
    );
    expect(screen.getByRole("link", { name: "via TechCrunch" })).toHaveAttribute(
      "href",
      "https://techcrunch.com/article",
    );
    expect(
      screen.getByText("potential competitor (AI-inferred)"),
    ).toBeInTheDocument();
  });

  it("renders the See-alternatives link only when a slug is provided", () => {
    const { rerender } = render(
      <Competitors competitors={[competitor()]} alternativesSlug="acme" />,
    );
    expect(
      screen.getByRole("link", { name: "See alternatives →" }),
    ).toHaveAttribute("href", "/alternatives/acme");

    rerender(<Competitors competitors={[competitor()]} />);
    expect(
      screen.queryByRole("link", { name: "See alternatives →" }),
    ).not.toBeInTheDocument();
  });
});

// ─── FundingHistory ───────────────────────────────────────────────────────────

describe("FundingHistory", () => {
  it("renders the empty state when there are no rounds", () => {
    render(<FundingHistory rounds={[]} />);
    expect(
      screen.getByText("No funding rounds recorded yet."),
    ).toBeInTheDocument();
  });

  it("marks only low-confidence rounds with the warning pill", () => {
    render(
      <FundingHistory
        rounds={[
          round({ round_type: "Seed", extraction_confidence: "low" }),
          round({ round_type: "Series B", extraction_confidence: "high" }),
        ]}
      />,
    );
    const pills = screen.getAllByText("low confidence");
    expect(pills).toHaveLength(1);
    expect(pills[0]).toHaveAttribute(
      "title",
      "Extracted with low confidence — treat as unverified",
    );
    // The pill sits in the Seed row, not the Series B row.
    expect(pills[0].closest("tr")).toHaveTextContent("Seed");
  });

  it("shows the rounded amount with the exact dollars in the title attribute", () => {
    render(<FundingHistory rounds={[round({ amount_raised: 15_100_000 })]} />);
    const amount = screen.getByText("$15.1M");
    expect(amount).toHaveAttribute("title", "$15,100,000");
  });

  it("shows the post-money valuation with its exact-dollar title", () => {
    render(
      <FundingHistory
        rounds={[round({ valuation_post_money: 1_500_000_000 })]}
      />,
    );
    const valuation = screen.getByText("$1.5B");
    expect(valuation).toHaveAttribute("title", "$1,500,000,000");
  });

  it("truncates long other-investor lists to three names and a count", () => {
    render(
      <FundingHistory
        rounds={[
          round({
            otherInvestors: ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"],
          }),
        ]}
      />,
    );
    expect(
      screen.getByText("Alpha, Beta, Gamma and 2 more"),
    ).toBeInTheDocument();
  });

  it("renders the freshness rider from asOf", () => {
    render(<FundingHistory rounds={[round()]} asOf="2026-03-01" />);
    expect(screen.getByText("latest round March 1, 2026")).toBeInTheDocument();
  });
});

// ─── Investors ────────────────────────────────────────────────────────────────

function companyInvestor(
  overrides: Partial<CompanyInvestorRow> = {},
): CompanyInvestorRow {
  return {
    name: "Some Fund",
    website: null,
    isLead: false,
    source: "vc_portfolio",
    ...overrides,
  };
}

describe("Investors", () => {
  it("renders the empty state when both sources are empty", () => {
    render(<Investors investors={[]} rounds={[]} />);
    expect(screen.getByText("No investors recorded yet.")).toBeInTheDocument();
  });

  it("dedups case-insensitively across company-level and round-level sources, keeping the first casing", () => {
    render(
      <Investors
        investors={[
          companyInvestor({
            name: "Sequoia Capital",
            website: "https://sequoiacap.com",
          }),
        ]}
        rounds={[round({ leadInvestors: ["sequoia capital"] })]}
      />,
    );
    const pills = screen.getAllByRole("listitem");
    expect(pills).toHaveLength(1);
    // Company-level casing wins; the round-level lead flag accumulates.
    const pill = within(pills[0]);
    expect(pill.getByText("Sequoia Capital")).toBeInTheDocument();
    expect(pill.getByText("lead")).toBeInTheDocument();
    // The company-level website survives the merge.
    expect(
      pill.getByRole("link", { name: "Sequoia Capital" }),
    ).toHaveAttribute("href", "https://sequoiacap.com");
  });

  it("sorts leads first, then alphabetically within each group", () => {
    render(
      <Investors
        investors={[companyInvestor({ name: "Mid Fund", isLead: false })]}
        rounds={[
          round({
            leadInvestors: ["Zeta Lead"],
            otherInvestors: ["Alpha Other"],
          }),
        ]}
      />,
    );
    const names = screen
      .getAllByRole("listitem")
      .map((li) => li.textContent ?? "");
    expect(names[0]).toContain("Zeta Lead");
    // Non-leads follow, alphabetical.
    expect(names.slice(1).map((n) => n.trim())).toEqual([
      "Alpha Other",
      "Mid Fund",
    ]);
  });

  it("prefers the on-site investor page over the firm website when the name resolves", () => {
    render(
      <Investors
        investors={[
          companyInvestor({
            name: "Alpha Capital",
            website: "https://alpha.example",
          }),
        ]}
        rounds={[]}
        nameToSlug={{ "alpha capital": "alpha-capital" }}
      />,
    );
    expect(
      screen.getByRole("link", { name: "Alpha Capital" }),
    ).toHaveAttribute("href", "/investor/alpha-capital");
  });
});

// ─── Sources ──────────────────────────────────────────────────────────────────

describe("Sources", () => {
  it("collapses citations that render to the same label + hostname", () => {
    render(
      <Sources
        citations={[
          { label: "Series A · $10M", url: "https://techcrunch.com/a?utm=1" },
          { label: "Series A · $10M", url: "https://techcrunch.com/b" },
        ]}
      />,
    );
    expect(screen.getAllByText("Series A · $10M")).toHaveLength(1);
    // First occurrence wins: the kept link is the first URL.
    expect(screen.getByRole("link", { name: "techcrunch.com" })).toHaveAttribute(
      "href",
      "https://techcrunch.com/a?utm=1",
    );
  });

  it("keeps distinct facts that cite the same URL", () => {
    render(
      <Sources
        citations={[
          { label: "Total raised · $40M", url: "https://techcrunch.com/a" },
          { label: "Series B · $40M", url: "https://techcrunch.com/a" },
        ]}
      />,
    );
    expect(screen.getByText("Total raised · $40M")).toBeInTheDocument();
    expect(screen.getByText("Series B · $40M")).toBeInTheDocument();
  });

  it("drops citations whose URL cannot be parsed", () => {
    render(
      <Sources
        citations={[
          { label: "Broken", url: "not a url at all" },
          { label: "Fine", url: "https://example.com/x" },
        ]}
      />,
    );
    expect(screen.queryByText("Broken")).not.toBeInTheDocument();
    expect(screen.getByText("Fine")).toBeInTheDocument();
  });

  it("renders nothing when no citation survives", () => {
    const { container } = render(
      <Sources citations={[{ label: "Broken", url: "::::" }]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("strips www. from the displayed hostname but links the full URL", () => {
    render(
      <Sources
        citations={[{ label: "Leadership", url: "https://www.example.com/team" }]}
      />,
    );
    const link = screen.getByRole("link", { name: "example.com" });
    expect(link).toHaveAttribute("href", "https://www.example.com/team");
  });
});

// ─── StatusBadge ──────────────────────────────────────────────────────────────

describe("StatusBadge", () => {
  it("renders nothing for active or unknown statuses", () => {
    const active = render(<StatusBadge status="active" />);
    expect(active.container).toBeEmptyDOMElement();
    const unknown = render(<StatusBadge status="zombie" />);
    expect(unknown.container).toBeEmptyDOMElement();
  });

  it("marks exits with a labeled, explained pill", () => {
    render(<StatusBadge status="acquired" />);
    const pill = screen.getByText("Acquired");
    expect(pill).toHaveAttribute("title", "This company has been acquired");
    expect(pill.closest("a")).toBeNull(); // no source → no link
  });

  it("labels shut_down and ipo statuses", () => {
    render(<StatusBadge status="shut_down" />);
    expect(screen.getByText("Shut down")).toBeInTheDocument();
    render(<StatusBadge status="ipo" />);
    expect(screen.getByText("IPO")).toBeInTheDocument();
  });

  it("links the pill to the announcement when a source URL is recorded", () => {
    render(
      <StatusBadge status="acquired" sourceUrl="https://news.example/deal" />,
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "https://news.example/deal");
    expect(within(link).getByText("Acquired")).toBeInTheDocument();
  });
});
