import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EventTimeline } from "@/components/EventTimeline";
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
    ...overrides,
  };
}

describe("EventTimeline coverage grouping", () => {
  it("collapses ≥2 sources into a disclosure listing every article, with no standalone ↗", () => {
    const r = round({
      announced_date: "2026-03-04",
      primary_news_url: "https://techcrunch.com/tc",
    });
    render(
      <EventTimeline
        rounds={[r]}
        news={[
          news({ url: "https://techcrunch.com/tc", title: "TC story", published_date: "2026-03-04" }),
          news({ url: "https://reuters.com/rt", title: "Reuters story", published_date: "2026-03-05" }),
          news({ url: "https://bloomberg.com/bb", title: "Bloomberg story", published_date: "2026-03-03" }),
        ]}
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
    // The near-duplicate articles are NOT separate news entries, and the inline
    // per-round ↗ is not also shown (the disclosure subsumes it).
    expect(
      screen.queryByRole("link", { name: /Source for Funding round/ }),
    ).not.toBeInTheDocument();
  });

  it("names DISTINCT outlets in the summary (never 'techcrunch.com, techcrunch.com')", () => {
    const r = round({ announced_date: "2026-03-04", primary_news_url: null });
    render(
      <EventTimeline
        rounds={[r]}
        news={[
          news({ url: "https://techcrunch.com/a", title: "TC A", published_date: "2026-03-05" }),
          news({ url: "https://techcrunch.com/b", title: "TC B", published_date: "2026-03-04" }),
          news({ url: "https://reuters.com/c", title: "RT C", published_date: "2026-03-03" }),
        ]}
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
    render(<EventTimeline rounds={[r]} news={[]} />);

    expect(
      screen.getByRole("link", { name: /Source for Funding round/ }),
    ).toHaveAttribute("href", "https://techcrunch.com/only");
    expect(screen.queryByText(/Covered by/)).not.toBeInTheDocument();
  });

  it("does NOT re-render a round's own article as a separate news entry", () => {
    const primaryUrl = "https://techcrunch.com/theround";
    const r = round({ announced_date: "2026-03-04", primary_news_url: primaryUrl });
    render(
      <EventTimeline
        rounds={[r]}
        news={[news({ url: primaryUrl, title: "The round", published_date: "2026-03-04" })]}
      />,
    );

    // The article backs the round (as its ↗ source), not a duplicate news row.
    expect(
      screen.queryByRole("link", { name: "The round" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Source for Funding round/ }),
    ).toBeInTheDocument();
  });

  it("still renders news that matches no round as its own entry", () => {
    const r = round({ announced_date: "2026-01-01", primary_news_url: null });
    render(
      <EventTimeline
        rounds={[r]}
        news={[
          news({
            url: "https://reuters.com/unrelated",
            title: "Unrelated coverage",
            published_date: "2026-06-01", // far outside any round's window
          }),
        ]}
      />,
    );

    expect(
      screen.getByRole("link", { name: "Unrelated coverage" }),
    ).toHaveAttribute("href", "https://reuters.com/unrelated");
  });
});
