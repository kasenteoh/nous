import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CompanyCard } from "@/components/CompanyCard";
import { Competitors } from "@/components/Competitors";
import { EventTimeline } from "@/components/EventTimeline";
import { Investors } from "@/components/Investors";
import {
  MOMENTUM_BADGE_THRESHOLD,
  MomentumBadge,
} from "@/components/MomentumBadge";
import { FounderBackground } from "@/components/FounderBackground";
import { PortfolioMomentum } from "@/components/PortfolioMomentum";
import { RelatedCompanies } from "@/components/RelatedCompanies";
import { Sources } from "@/components/Sources";
import { StatusBadge } from "@/components/StatusBadge";
import type {
  CareerMove,
  CompanyInvestorRow,
  CompanyListRow,
  CompetitorWithResolved,
  FundingRoundWithInvestors,
  NewsArticleRow,
  RelatedCompany,
  SimilarCompany,
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

// ─── EventTimeline ────────────────────────────────────────────────────────────

function newsArticle(
  overrides: Partial<NewsArticleRow> = {},
): NewsArticleRow {
  fixtureSeq += 1;
  return {
    id: `news-${fixtureSeq}`,
    url: `https://example.test/article-${fixtureSeq}`,
    title: `Article ${fixtureSeq}`,
    source: "techcrunch.com",
    published_date: "2026-02-01",
    ...overrides,
  };
}

describe("EventTimeline", () => {
  it("renders the empty state when there are no rounds or news", () => {
    render(<EventTimeline rounds={[]} news={[]} />);
    expect(
      screen.getByText("No funding rounds or news recorded yet."),
    ).toBeInTheDocument();
  });

  it("marks only low-confidence rounds with the warning pill", () => {
    render(
      <EventTimeline
        rounds={[
          round({ round_type: "Seed", extraction_confidence: "low" }),
          round({ round_type: "Series B", extraction_confidence: "high" }),
        ]}
        news={[]}
      />,
    );
    const pills = screen.getAllByText("low confidence");
    expect(pills).toHaveLength(1);
    expect(pills[0]).toHaveAttribute(
      "title",
      "Extracted with low confidence — treat as unverified",
    );
    // The pill sits in the Seed entry, not the Series B entry.
    expect(pills[0].closest("li")).toHaveTextContent("Seed");
  });

  it("shows the rounded amount with the exact dollars in the title attribute", () => {
    render(
      <EventTimeline
        rounds={[round({ amount_raised: 15_100_000 })]}
        news={[]}
      />,
    );
    const amount = screen.getByText("$15.1M");
    expect(amount).toHaveAttribute("title", "$15,100,000");
  });

  it("shows the post-money valuation with its exact-dollar title", () => {
    render(
      <EventTimeline
        rounds={[round({ valuation_post_money: 1_500_000_000 })]}
        news={[]}
      />,
    );
    const valuation = screen.getByText("$1.5B");
    expect(valuation).toHaveAttribute("title", "$1,500,000,000");
  });

  it("truncates long other-investor lists to three names and a count", () => {
    render(
      <EventTimeline
        rounds={[
          round({
            otherInvestors: ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"],
          }),
        ]}
        news={[]}
      />,
    );
    expect(
      screen.getByText(/Alpha, Beta, Gamma and 2 more/),
    ).toBeInTheDocument();
  });

  it("links news entries out to the source article", () => {
    render(
      <EventTimeline
        rounds={[]}
        news={[newsArticle({ title: "Big raise coverage", url: "https://news.test/x" })]}
      />,
    );
    const link = screen.getByRole("link", { name: "Big raise coverage" });
    expect(link).toHaveAttribute("href", "https://news.test/x");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("interleaves funding + news newest-first by date", () => {
    render(
      <EventTimeline
        rounds={[round({ round_type: "Series A", announced_date: "2026-03-10" })]}
        news={[
          newsArticle({ title: "Older news", published_date: "2026-01-05" }),
          newsArticle({ title: "Newest news", published_date: "2026-05-20" }),
        ]}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    // Order: Newest news (May) → Series A (Mar) → Older news (Jan).
    expect(items[0]).toHaveTextContent("Newest news");
    expect(items[1]).toHaveTextContent("Series A");
    expect(items[2]).toHaveTextContent("Older news");
  });

  it("floats an undated funding round to the top and sinks undated news to the bottom", () => {
    render(
      <EventTimeline
        rounds={[round({ round_type: "Series H", announced_date: null })]}
        news={[
          newsArticle({ title: "Dated news", published_date: "2026-05-20" }),
          newsArticle({ title: "Undated news", published_date: null }),
        ]}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    // Undated funding leads, dated news middle, undated news trails.
    expect(items[0]).toHaveTextContent("Series H");
    expect(items[1]).toHaveTextContent("Dated news");
    expect(items[2]).toHaveTextContent("Undated news");
  });

  it("omits the 'Led by' clause when only non-lead investors are present", () => {
    render(
      <EventTimeline
        rounds={[
          round({ leadInvestors: [], otherInvestors: ["Alpha", "Beta"] }),
        ]}
        news={[]}
      />,
    );
    // No bare "Led by —"; the other investors still show.
    expect(screen.queryByText(/Led by/)).not.toBeInTheDocument();
    expect(screen.getByText(/Alpha, Beta/)).toBeInTheDocument();
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

// ─── MomentumBadge ────────────────────────────────────────────────────────────

describe("MomentumBadge", () => {
  it("renders the pill at the threshold, with the explaining title", () => {
    const { container } = render(
      <MomentumBadge score={MOMENTUM_BADGE_THRESHOLD} />,
    );
    const pill = within(container).getByText("🔥 Heating up");
    expect(pill).toHaveAttribute(
      "title",
      "Momentum is accelerating — recent hiring, news, and funding activity",
    );
  });

  it("renders the pill for scores well above the threshold", () => {
    const { container } = render(<MomentumBadge score={0.95} />);
    expect(within(container).getByText("🔥 Heating up")).toBeInTheDocument();
  });

  it("renders nothing below the threshold or for null/undefined scores", () => {
    const below = render(
      <MomentumBadge score={MOMENTUM_BADGE_THRESHOLD - 0.01} />,
    );
    expect(below.container).toBeEmptyDOMElement();

    const nullScore = render(<MomentumBadge score={null} />);
    expect(nullScore.container).toBeEmptyDOMElement();

    const undefinedScore = render(<MomentumBadge score={undefined} />);
    expect(undefinedScore.container).toBeEmptyDOMElement();
  });
});

// ─── CompanyCard (momentum props gating) ──────────────────────────────────────

function companyListRow(overrides: Partial<CompanyListRow> = {}): CompanyListRow {
  return {
    slug: "acme",
    name: "Acme",
    hq_city: "San Francisco",
    hq_state: "CA",
    industry_group: "Fintech",
    description_short: "Payments infra.",
    status: "active",
    logo_url: null,
    ...overrides,
  };
}

describe("CompanyCard momentum props", () => {
  it("shows the badge and the joined why line when momentum props are supplied", () => {
    render(
      <CompanyCard
        company={companyListRow()}
        momentumScore={0.82}
        momentumWhy={["+40% team", "5 news mentions"]}
      />,
    );
    expect(screen.getByText("🔥 Heating up")).toBeInTheDocument();
    expect(
      screen.getByText("+40% team · 5 news mentions"),
    ).toBeInTheDocument();
  });

  it("renders neither the badge nor a why line when the props are omitted (every non-/trending call site)", () => {
    render(<CompanyCard company={companyListRow()} />);
    expect(screen.queryByText("🔥 Heating up")).not.toBeInTheDocument();
    expect(screen.queryByText(/·/)).not.toBeInTheDocument();
  });

  it("omits the badge when the score is below threshold even if a why line is present", () => {
    render(
      <CompanyCard
        company={companyListRow()}
        momentumScore={MOMENTUM_BADGE_THRESHOLD - 0.1}
        momentumWhy={["small bump"]}
      />,
    );
    expect(screen.queryByText("🔥 Heating up")).not.toBeInTheDocument();
    // The why line is independent of the badge threshold — it still renders.
    expect(screen.getByText("small bump")).toBeInTheDocument();
  });
});

// ─── RelatedCompanies ─────────────────────────────────────────────────────────

function heuristicSimilar(
  overrides: Partial<RelatedCompany> = {},
): RelatedCompany {
  fixtureSeq += 1;
  return {
    slug: `edge-co-${fixtureSeq}`,
    name: `Edge Co ${fixtureSeq}`,
    descriptionShort: "Heuristic-graph neighbor.",
    status: "active",
    industryGroup: "developer-tools",
    score: 0.5,
    evidence: "Both in developer-tools; 3 shared tags",
    ...overrides,
  };
}

function embeddingSimilar(
  overrides: Partial<SimilarCompany> = {},
): SimilarCompany {
  fixtureSeq += 1;
  return {
    slug: `vec-co-${fixtureSeq}`,
    name: `Vec Co ${fixtureSeq}`,
    logoUrl: null,
    descriptionShort: "Embedding neighbor.",
    industryGroup: "data-infrastructure",
    similarity: 0.87,
    ...overrides,
  };
}

describe("RelatedCompanies", () => {
  it("renders nothing when every list is empty", () => {
    const { container } = render(
      <RelatedCompanies similar={[]} similarByDescription={[]} alsoBackedBy={[]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("prefers embedding neighbors and captions each card with its similarity", () => {
    const embedding = embeddingSimilar({
      slug: "vector-co",
      name: "Vector Co",
      similarity: 0.87,
    });
    const heuristic = heuristicSimilar({ slug: "tag-co", name: "Tag Co" });
    render(
      <RelatedCompanies
        similar={[heuristic]}
        similarByDescription={[embedding]}
        alsoBackedBy={[]}
      />,
    );

    // The embedding card links to the company and discloses its derivation.
    const link = screen.getByRole("link", { name: "Vector Co" });
    expect(link).toHaveAttribute("href", "/c/vector-co");
    expect(screen.getByText("87% description similarity")).toBeInTheDocument();

    // The weaker heuristic edges are replaced, not blended — one list, one
    // ranking principle at a time.
    expect(screen.queryByText("Tag Co")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Both in developer-tools; 3 shared tags"),
    ).not.toBeInTheDocument();
  });

  it("clamps the similarity caption to 99% (float rounding must not overclaim)", () => {
    render(
      <RelatedCompanies
        similar={[]}
        similarByDescription={[embeddingSimilar({ similarity: 0.9999 })]}
        alsoBackedBy={[]}
      />,
    );
    expect(screen.getByText("99% description similarity")).toBeInTheDocument();
  });

  it("falls back to heuristic edges (with their evidence caption) when no embedding neighbors exist", () => {
    const heuristic = heuristicSimilar({ slug: "tag-co", name: "Tag Co" });
    render(
      <RelatedCompanies
        similar={[heuristic]}
        similarByDescription={[]}
        alsoBackedBy={[]}
      />,
    );

    const link = screen.getByRole("link", { name: "Tag Co" });
    expect(link).toHaveAttribute("href", "/c/tag-co");
    expect(
      screen.getByText("Both in developer-tools; 3 shared tags"),
    ).toBeInTheDocument();
  });

  it("renders the also-backed-by list alongside embedding neighbors", () => {
    render(
      <RelatedCompanies
        similar={[]}
        similarByDescription={[embeddingSimilar()]}
        alsoBackedBy={[
          { slug: "sibling-co", name: "Sibling Co", sharedInvestors: ["Seed Fund"] },
        ]}
      />,
    );
    expect(screen.getByRole("link", { name: "Sibling Co" })).toHaveAttribute(
      "href",
      "/c/sibling-co",
    );
    expect(screen.getByText("Also backed by Seed Fund")).toBeInTheDocument();
  });
});

// ─── FounderBackground ────────────────────────────────────────────────────────

function careerMove(overrides: Partial<CareerMove> = {}): CareerMove {
  return {
    personName: "Jane Doe",
    priorCompanyName: "Stripe",
    priorRole: "Engineer",
    startYear: null,
    endYear: null,
    priorCompanySlug: null,
    ...overrides,
  };
}

describe("FounderBackground", () => {
  it("renders nothing when empty", () => {
    const { container } = render(<FounderBackground careerMoves={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("links a resolved prior company and leaves an unresolved one as text", () => {
    render(
      <FounderBackground
        careerMoves={[
          careerMove({ priorCompanyName: "Oracle", priorCompanySlug: "oracle" }),
          careerMove({ priorCompanyName: "Sun Microsystems", priorCompanySlug: null }),
        ]}
      />,
    );
    expect(screen.getByRole("link", { name: "Oracle" })).toHaveAttribute(
      "href",
      "/c/oracle",
    );
    expect(screen.queryByRole("link", { name: "Sun Microsystems" })).toBeNull();
    expect(screen.getByText("Sun Microsystems")).toBeInTheDocument();
  });

  it("groups multiple prior employers under one founder", () => {
    render(
      <FounderBackground
        careerMoves={[
          careerMove({ personName: "Jane Doe", priorCompanyName: "Stripe" }),
          careerMove({ personName: "Jane Doe", priorCompanyName: "Google" }),
        ]}
      />,
    );
    // One heading for the founder, two employer entries.
    expect(screen.getAllByText("Jane Doe")).toHaveLength(1);
    expect(screen.getByText("Stripe")).toBeInTheDocument();
    expect(screen.getByText("Google")).toBeInTheDocument();
  });

  it("never claims 'present' for a prior employer with an unknown end year", () => {
    render(
      <FounderBackground
        careerMoves={[careerMove({ startYear: 2005, endYear: null })]}
      />,
    );
    expect(screen.getByText(/from 2005/)).toBeInTheDocument();
    expect(screen.queryByText(/present/)).toBeNull();
    expect(screen.queryByText(/\?/)).toBeNull();
  });

  it("renders a full span and a start-only / end-only tenure honestly", () => {
    const { rerender } = render(
      <FounderBackground
        careerMoves={[careerMove({ startYear: 2010, endYear: 2014 })]}
      />,
    );
    expect(screen.getByText(/2010–2014/)).toBeInTheDocument();

    rerender(
      <FounderBackground
        careerMoves={[careerMove({ startYear: null, endYear: 2014 })]}
      />,
    );
    expect(screen.getByText(/until 2014/)).toBeInTheDocument();
    expect(screen.queryByText(/\?/)).toBeNull();
  });
});

// ─── PortfolioMomentum ────────────────────────────────────────────────────────

describe("PortfolioMomentum", () => {
  it("renders nothing when null or nothing is heating up", () => {
    const { container, rerender } = render(<PortfolioMomentum momentum={null} />);
    expect(container).toBeEmptyDOMElement();
    rerender(
      <PortfolioMomentum
        momentum={{ scoredCount: 5, heatingUpCount: 0, meanMomentum: 0.5, topHeatingUp: [] }}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("summarizes the heating-up count and links the hot companies with their why", () => {
    render(
      <PortfolioMomentum
        momentum={{
          scoredCount: 42,
          heatingUpCount: 2,
          meanMomentum: 0.61,
          topHeatingUp: [
            { slug: "rocket", name: "Rocket", momentumScore: 0.9, momentumWhy: ["news +180%"] },
            { slug: "surge", name: "Surge", momentumScore: 0.8, momentumWhy: [] },
          ],
        }}
      />,
    );
    expect(screen.getByText(/2 of 42 scored portfolio companies heating up/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Rocket" })).toHaveAttribute("href", "/c/rocket");
    expect(screen.getByText("news +180%")).toBeInTheDocument();
  });

  it("pluralizes the noun on the denominator (1 of 42 → 'companies')", () => {
    render(
      <PortfolioMomentum
        momentum={{
          scoredCount: 42,
          heatingUpCount: 1,
          meanMomentum: 0.55,
          topHeatingUp: [
            { slug: "solo", name: "Solo", momentumScore: 0.9, momentumWhy: [] },
          ],
        }}
      />,
    );
    expect(
      screen.getByText(/1 of 42 scored portfolio companies heating up/),
    ).toBeInTheDocument();
  });
});
