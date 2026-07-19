import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FundingTimeline, type FundingItem } from "@/components/FundingTimeline";
import { buildTimeline } from "@/lib/timeline";
import type { FundingRoundWithInvestors, NewsArticleRow } from "@/lib/types";

let seq = 0;

function round(
  overrides: Partial<FundingRoundWithInvestors> = {},
): FundingRoundWithInvestors {
  seq += 1;
  return {
    id: `r-${seq}`,
    company_id: "c-1",
    round_type: "Series B",
    amount_raised: 200_000_000,
    valuation_post_money: null,
    valuation_source: null,
    announced_date: "2026-03-04",
    primary_news_url: null,
    extraction_confidence: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    leadInvestors: [],
    otherInvestors: [],
    ...overrides,
  };
}

function news(overrides: Partial<NewsArticleRow> = {}): NewsArticleRow {
  seq += 1;
  return {
    id: `n-${seq}`,
    url: `https://techcrunch.com/story-${seq}`,
    title: `Headline ${seq}`,
    source: "techcrunch.com",
    published_date: "2026-03-04",
    funding_round_id: null,
    ...overrides,
  };
}

/** The page's split: buildTimeline once, funding items only. */
function fundingItems(
  rounds: FundingRoundWithInvestors[],
  articles: NewsArticleRow[],
): FundingItem[] {
  return buildTimeline(rounds, articles).filter(
    (item): item is FundingItem => item.kind === "funding",
  );
}

describe("FundingTimeline", () => {
  it("renders the Funding section header and only funding rows", () => {
    const r = round({ round_type: "Series A", announced_date: "2026-03-04" });
    render(
      <FundingTimeline
        items={fundingItems(
          [r],
          [
            news({
              url: "https://reuters.com/standalone",
              title: "Standalone story",
              published_date: "2026-06-01", // outside any round's window
            }),
          ],
        )}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Funding" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Series A")).toBeInTheDocument();
    // The standalone article belongs to NewsSection, never this rail.
    expect(
      screen.queryByRole("link", { name: "Standalone story" }),
    ).not.toBeInTheDocument();
  });

  it("renders nothing at all when there are no rounds", () => {
    const { container } = render(<FundingTimeline items={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("collapses ≥2 sources into a disclosure listing every article, with no standalone ↗", () => {
    const r = round({
      announced_date: "2026-03-04",
      primary_news_url: "https://techcrunch.com/tc",
    });
    render(
      <FundingTimeline
        items={fundingItems(
          [r],
          [
            news({ url: "https://techcrunch.com/tc", title: "TC story", published_date: "2026-03-04" }),
            news({ url: "https://reuters.com/rt", title: "Reuters story", published_date: "2026-03-05" }),
            news({ url: "https://bloomberg.com/bb", title: "Bloomberg story", published_date: "2026-03-03" }),
          ],
        )}
      />,
    );

    // The collapsed summary names the first two hosts + a "+N more sources" count.
    expect(screen.getByText(/Covered by/)).toHaveTextContent(
      "Covered by techcrunch.com, reuters.com +1 more source",
    );
    // Every article is one click away in the expanded list.
    expect(screen.getByRole("link", { name: /Reuters story/ })).toHaveAttribute(
      "href",
      "https://reuters.com/rt",
    );
    expect(screen.getByRole("link", { name: /Bloomberg story/ })).toHaveAttribute(
      "href",
      "https://bloomberg.com/bb",
    );
    // The near-duplicate articles are NOT separate rows, and the inline
    // per-round ↗ is not also shown (the disclosure subsumes it).
    expect(
      screen.queryByRole("link", { name: /Source for Funding round/ }),
    ).not.toBeInTheDocument();
  });

  it("names DISTINCT outlets in the summary (never 'techcrunch.com, techcrunch.com')", () => {
    const r = round({ announced_date: "2026-03-04", primary_news_url: null });
    render(
      <FundingTimeline
        items={fundingItems(
          [r],
          [
            news({ url: "https://techcrunch.com/a", title: "TC A", published_date: "2026-03-05" }),
            news({ url: "https://techcrunch.com/b", title: "TC B", published_date: "2026-03-04" }),
            news({ url: "https://reuters.com/c", title: "RT C", published_date: "2026-03-03" }),
          ],
        )}
      />,
    );
    // 3 articles, 2 distinct outlets → the summary names both once, no "+N more".
    const summary = screen.getByText(/Covered by/);
    expect(summary).toHaveTextContent("Covered by techcrunch.com, reuters.com");
    expect(summary.textContent).not.toContain("techcrunch.com, techcrunch.com");
    expect(summary.textContent).not.toContain("more source");
    // Both techcrunch articles are still individually listed on expand.
    expect(screen.getByRole("link", { name: /TC A/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /TC B/ })).toBeInTheDocument();
  });

  it("keeps the single inline ↗ (no disclosure) for a round with one source", () => {
    const r = round({
      announced_date: "2026-03-04",
      primary_news_url: "https://techcrunch.com/only",
    });
    render(<FundingTimeline items={fundingItems([r], [])} />);

    expect(
      screen.getByRole("link", { name: /Source for Funding round/ }),
    ).toHaveAttribute("href", "https://techcrunch.com/only");
    expect(screen.queryByText(/Covered by/)).not.toBeInTheDocument();
  });

  it("does NOT render a round's own article as a story row anywhere", () => {
    const primaryUrl = "https://techcrunch.com/theround";
    const r = round({ announced_date: "2026-03-04", primary_news_url: primaryUrl });
    render(
      <FundingTimeline
        items={fundingItems(
          [r],
          [news({ url: primaryUrl, title: "The round", published_date: "2026-03-04" })],
        )}
      />,
    );

    // The article backs the round (as its ↗ source), not a duplicate row.
    expect(
      screen.queryByRole("link", { name: "The round" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Source for Funding round/ }),
    ).toBeInTheDocument();
  });

  it("shows the low-confidence pill only for low-confidence rounds", () => {
    render(
      <FundingTimeline
        items={fundingItems(
          [
            round({ extraction_confidence: "low", announced_date: "2026-03-04" }),
            round({ extraction_confidence: "high", announced_date: "2026-01-10" }),
          ],
          [],
        )}
      />,
    );
    expect(screen.getAllByText("low confidence")).toHaveLength(1);
  });
});
