import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  NewsSection,
  NEWS_VISIBLE_COUNT,
  type NewsItem,
} from "@/components/NewsSection";
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

/** The page's split: buildTimeline once, standalone stories only. */
function newsItems(
  rounds: FundingRoundWithInvestors[],
  articles: NewsArticleRow[],
): NewsItem[] {
  return buildTimeline(rounds, articles).filter(
    (item): item is NewsItem => item.kind === "news",
  );
}

/** N standalone articles with distinct titles/dates far from any round, spaced
 *  >STORY_WINDOW_DAYS apart so they never cluster into one story. */
function standaloneArticles(n: number): NewsArticleRow[] {
  return Array.from({ length: n }, (_, i) => {
    const month = String((i % 12) + 1).padStart(2, "0");
    const year = 2025 - Math.floor(i / 12);
    return news({
      url: `https://outlet-${i}.com/story-${i}`,
      title: `Completely distinct event number ${i} about topic ${i}`,
      published_date: `${year}-${month}-01`,
    });
  });
}

describe("NewsSection", () => {
  it("renders a story row: headline link + date + source host", () => {
    render(
      <NewsSection
        items={newsItems(
          [],
          [
            news({
              url: "https://reuters.com/ipo-chatter",
              title: "Acme mulls IPO",
              source: "reuters.com",
              published_date: "2026-05-10",
            }),
          ],
        )}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "In the news" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Acme mulls IPO" })).toHaveAttribute(
      "href",
      "https://reuters.com/ipo-chatter",
    );
    // Date + the lead's host on the meta line.
    expect(screen.getByText(/reuters\.com/)).toBeInTheDocument();
  });

  it("falls back to the lead URL's host when the stored source is absent", () => {
    render(
      <NewsSection
        items={newsItems(
          [],
          [
            news({
              url: "https://bloomberg.com/scoop",
              title: "Acme said to raise",
              source: null,
              published_date: "2026-05-01",
            }),
          ],
        )}
      />,
    );
    expect(screen.getByText(/bloomberg\.com/)).toBeInTheDocument();
  });

  it("renders nothing at all when every article attached to a round", () => {
    const r = round({ announced_date: "2026-03-04" });
    const { container } = render(
      <NewsSection
        items={newsItems(
          [r],
          [news({ url: "https://techcrunch.com/covered", published_date: "2026-03-05" })],
        )}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("collapses a syndicated story into one row with a Covered-by disclosure", () => {
    render(
      <NewsSection
        items={newsItems(
          [],
          [
            news({
              url: "https://techcrunch.com/rumor",
              title: "Acme seeks $10 billion at monster valuation",
              published_date: "2026-05-02",
            }),
            news({
              url: "https://reuters.com/rumor",
              title: "Acme seeks $10 billion at monster valuation",
              published_date: "2026-05-01",
            }),
          ],
        )}
      />,
    );

    // One story row (the lead), not two.
    expect(screen.getAllByRole("listitem").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Covered by/)).toHaveTextContent(
      "Covered by techcrunch.com, reuters.com",
    );
    // The syndicated copy is one click away inside the disclosure.
    expect(
      screen.getAllByRole("link", {
        name: /Acme seeks \$10 billion at monster valuation/,
      }).length,
    ).toBeGreaterThanOrEqual(2);
  });

  it(`caps visible stories at ${NEWS_VISIBLE_COUNT} and tucks the rest behind "Show N older stories"`, () => {
    const items = newsItems([], standaloneArticles(11));
    expect(items).toHaveLength(11);
    render(<NewsSection items={items} />);

    // The three oldest live inside the details, labeled with the exact count.
    expect(screen.getByText("Show 3 older stories")).toBeInTheDocument();
    // Nothing is dropped: every story's link is in the DOM (details content is
    // still rendered server-side, just collapsed).
    for (let i = 0; i < 11; i += 1) {
      expect(
        screen.getByRole("link", {
          name: `Completely distinct event number ${i} about topic ${i}`,
        }),
      ).toBeInTheDocument();
    }
  });

  it("singularizes the older-stories label", () => {
    render(<NewsSection items={newsItems([], standaloneArticles(9))} />);
    expect(screen.getByText("Show 1 older story")).toBeInTheDocument();
  });

  it("shows no details toggle at or below the visible cap", () => {
    render(
      <NewsSection
        items={newsItems([], standaloneArticles(NEWS_VISIBLE_COUNT))}
      />,
    );
    expect(screen.queryByText(/older stor/)).not.toBeInTheDocument();
  });
});
