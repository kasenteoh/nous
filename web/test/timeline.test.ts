import { describe, expect, it } from "vitest";
import { buildTimeline, MATCH_WINDOW_DAYS } from "@/lib/timeline";
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

/** The single funding item's coverage (asserts exactly one funding item). */
function coverageOf(
  rounds: FundingRoundWithInvestors[],
  articles: NewsArticleRow[],
) {
  const items = buildTimeline(rounds, articles);
  const funding = items.filter((i) => i.kind === "funding");
  expect(funding).toHaveLength(rounds.length);
  return funding[0].kind === "funding" ? funding[0].coverage : [];
}

function standaloneNews(
  rounds: FundingRoundWithInvestors[],
  articles: NewsArticleRow[],
) {
  return buildTimeline(rounds, articles).filter((i) => i.kind === "news");
}

describe("buildTimeline clustering", () => {
  it("attaches a near-dated article to the round it covers", () => {
    const r = round({ announced_date: "2026-03-04" });
    const cov = coverageOf(
      [r],
      [news({ published_date: "2026-03-05", url: "https://reuters.com/a" })],
    );
    expect(cov.map((c) => c.host)).toContain("reuters.com");
    expect(standaloneNews([r], [news({ published_date: "2026-03-05" })])).toHaveLength(
      0,
    );
  });

  it("honors the ±window boundary (in at the edge, out beyond it)", () => {
    const r = round({ announced_date: "2026-03-04" });
    const atEdge = news({
      published_date: "2026-03-18", // exactly +14 days
      url: "https://reuters.com/edge",
    });
    const beyond = news({
      published_date: "2026-03-19", // +15 days
      url: "https://bloomberg.com/beyond",
    });
    expect(MATCH_WINDOW_DAYS).toBe(14);
    expect(coverageOf([r], [atEdge]).map((c) => c.host)).toContain("reuters.com");
    // Beyond the window → standalone news, not coverage.
    expect(coverageOf([r], [beyond])).toHaveLength(0);
    expect(standaloneNews([r], [beyond])).toHaveLength(1);
  });

  it("assigns an article to the NEAREST round when several are in-window", () => {
    const early = round({
      id: "r-early",
      announced_date: "2026-03-01",
      primary_news_url: null,
    });
    const late = round({
      id: "r-late",
      announced_date: "2026-03-10",
      primary_news_url: null,
    });
    const article = news({
      published_date: "2026-03-09", // 1 day from late, 8 from early
      url: "https://reuters.com/near-late",
    });
    const items = buildTimeline([early, late], [article]);
    const lateItem = items.find(
      (i) => i.kind === "funding" && i.round.id === "r-late",
    );
    const earlyItem = items.find(
      (i) => i.kind === "funding" && i.round.id === "r-early",
    );
    expect(lateItem?.kind === "funding" && lateItem.coverage).toHaveLength(1);
    expect(earlyItem?.kind === "funding" && earlyItem.coverage).toHaveLength(0);
  });

  it("breaks an equal-distance tie toward the larger round", () => {
    const small = round({
      id: "r-small",
      announced_date: "2026-03-02",
      amount_raised: 5_000_000,
      primary_news_url: null,
    });
    const big = round({
      id: "r-big",
      announced_date: "2026-03-06",
      amount_raised: 500_000_000,
      primary_news_url: null,
    });
    const article = news({
      published_date: "2026-03-04", // 2 days from each
      url: "https://reuters.com/tie",
    });
    const items = buildTimeline([small, big], [article]);
    const bigItem = items.find(
      (i) => i.kind === "funding" && i.round.id === "r-big",
    );
    expect(bigItem?.kind === "funding" && bigItem.coverage).toHaveLength(1);
  });

  it("keeps an article standalone when it has no date, no in-window round, or no dated round", () => {
    const r = round({ announced_date: "2026-03-04", primary_news_url: null });
    // No published_date.
    expect(standaloneNews([r], [news({ published_date: null })])).toHaveLength(1);
    // No dated round at all.
    const undatedRound = round({
      announced_date: null,
      primary_news_url: null,
    });
    expect(
      standaloneNews([undatedRound], [news({ published_date: "2026-03-04" })]),
    ).toHaveLength(1);
    // A round with a null date is never a match candidate.
    expect(coverageOf([undatedRound], [])).toHaveLength(0);
  });
});

describe("buildTimeline coverage assembly", () => {
  it("collapses the round's own article (primary_news_url) — no double render", () => {
    const primaryUrl = "https://techcrunch.com/the-round";
    const r = round({
      announced_date: "2026-03-04",
      primary_news_url: primaryUrl,
    });
    // The same article is also a news_articles row (the common case).
    const article = news({ url: primaryUrl, published_date: "2026-03-04" });
    const items = buildTimeline([r], [article]);
    // It appears ONCE, as the round's coverage — not also as a standalone news row.
    expect(items.filter((i) => i.kind === "news")).toHaveLength(0);
    const cov = items[0].kind === "funding" ? items[0].coverage : [];
    expect(cov).toHaveLength(1);
    expect(cov[0].url).toBe(primaryUrl);
  });

  it("puts the primary source first, then the rest newest-first", () => {
    const primaryUrl = "https://techcrunch.com/primary";
    const r = round({
      announced_date: "2026-03-04",
      primary_news_url: primaryUrl,
    });
    const older = news({
      url: "https://reuters.com/older",
      published_date: "2026-03-02",
    });
    const primaryArticle = news({ url: primaryUrl, published_date: "2026-03-04" });
    const newer = news({
      url: "https://bloomberg.com/newer",
      published_date: "2026-03-06",
    });
    const cov = coverageOf([r], [older, primaryArticle, newer]);
    // Primary leads regardless of its date; the rest are newest-first.
    expect(cov.map((c) => c.host)).toEqual([
      "techcrunch.com",
      "bloomberg.com",
      "reuters.com",
    ]);
  });

  it("prepends a title-less entry for a primary_news_url with no news row", () => {
    const r = round({
      announced_date: "2026-03-04",
      primary_news_url: "https://wsj.com/exclusive",
    });
    const other = news({
      url: "https://reuters.com/coverage",
      published_date: "2026-03-04",
    });
    const cov = coverageOf([r], [other]);
    expect(cov[0].host).toBe("wsj.com");
    expect(cov[0].title).toBeNull(); // no news_articles row → host-only
    expect(cov[1].host).toBe("reuters.com");
  });

  it("dedups coverage by canonical URL (http/https/www/trailing-slash/query)", () => {
    const r = round({ announced_date: "2026-03-04", primary_news_url: null });
    const cov = coverageOf(
      [r],
      [
        news({ url: "https://www.reuters.com/story/", published_date: "2026-03-04" }),
        news({ url: "http://reuters.com/story?utm=x", published_date: "2026-03-04" }),
      ],
    );
    expect(cov).toHaveLength(1);
    expect(cov[0].host).toBe("reuters.com");
  });

  it("drops an unparseable / scheme-less coverage URL (no dead link)", () => {
    const r = round({ announced_date: "2026-03-04", primary_news_url: null });
    const cov = coverageOf(
      [r],
      [
        news({ url: "reuters.com/bare", published_date: "2026-03-04" }), // scheme-less
        news({ url: "https://bloomberg.com/ok", published_date: "2026-03-04" }),
      ],
    );
    expect(cov.map((c) => c.host)).toEqual(["bloomberg.com"]);
  });
});

describe("buildTimeline — primary pinning & unrenderable URLs (review fixes)", () => {
  it("excludes an unrenderable-URL article consistently — neither coverage NOR standalone", () => {
    const r = round({ announced_date: "2026-03-04", primary_news_url: null });
    // In-window date, but the URL can't render a real link.
    const bad = news({ url: "not a url", published_date: "2026-03-04" });
    const items = buildTimeline([r], [bad]);
    expect(items.filter((i) => i.kind === "news")).toHaveLength(0);
    expect(items[0].kind === "funding" ? items[0].coverage : []).toHaveLength(0);
  });

  it("pins a round's primary article to THAT round even when its news row is undated", () => {
    const primaryUrl = "https://techcrunch.com/announce";
    const r = round({ announced_date: "2026-03-04", primary_news_url: primaryUrl });
    // The primary's own news row has NO date — before the fix this went standalone
    // AND the round prepended a title-less copy → the URL rendered twice.
    const article = news({
      url: primaryUrl,
      title: "The announcement",
      published_date: null,
    });
    const items = buildTimeline([r], [article]);
    expect(items.filter((i) => i.kind === "news")).toHaveLength(0);
    const cov = items[0].kind === "funding" ? items[0].coverage : [];
    expect(cov).toHaveLength(1);
    expect(cov[0].url).toBe(primaryUrl);
    expect(cov[0].title).toBe("The announcement"); // uses the news row (not title-less)
  });

  it("pins a round's primary to its OWN round, not a date-nearer neighbor (no cross-attribution)", () => {
    const primaryUrl = "https://techcrunch.com/round-a";
    const roundA = round({
      id: "r-a",
      announced_date: "2026-03-01",
      primary_news_url: primaryUrl,
    });
    const roundB = round({
      id: "r-b",
      announced_date: "2026-03-10",
      primary_news_url: null,
    });
    // A's announcement is dated 03-09 — NEAREST to B, but it is A's primary source.
    const article = news({
      url: primaryUrl,
      title: "A raised",
      published_date: "2026-03-09",
    });
    const items = buildTimeline([roundA, roundB], [article]);
    const a = items.find((i) => i.kind === "funding" && i.round.id === "r-a");
    const b = items.find((i) => i.kind === "funding" && i.round.id === "r-b");
    expect(a?.kind === "funding" ? a.coverage.map((c) => c.url) : []).toContain(
      primaryUrl,
    );
    expect(b?.kind === "funding" ? b.coverage : []).toHaveLength(0);
    expect(items.filter((i) => i.kind === "news")).toHaveLength(0);
  });

  it("attaches by the persisted funding_round_id even to an UNDATED round (the exact link beats date proximity)", () => {
    // The motivating case: an undated round can never win date clustering, so
    // pre-0044 its coverage rendered as standalone news clutter.
    const undated = round({
      id: "r-undated",
      announced_date: null,
      primary_news_url: null,
    });
    const datedNeighbor = round({ id: "r-near", announced_date: "2026-03-05" });
    const article = news({
      published_date: "2026-03-04", // date-nearest to r-near…
      funding_round_id: "r-undated", // …but the pipeline KNOWS it covers r-undated
    });
    const items = buildTimeline([undated, datedNeighbor], [article]);
    const target = items.find(
      (i) => i.kind === "funding" && i.round.id === "r-undated",
    );
    const neighbor = items.find(
      (i) => i.kind === "funding" && i.round.id === "r-near",
    );
    expect(
      target?.kind === "funding" ? target.coverage.map((c) => c.url) : [],
    ).toContain(article.url);
    expect(neighbor?.kind === "funding" ? neighbor.coverage : []).toHaveLength(0);
    expect(items.filter((i) => i.kind === "news")).toHaveLength(0);
  });

  it("falls back to date clustering when funding_round_id is orphaned (round deleted/merged)", () => {
    const r = round({ id: "r-live", announced_date: "2026-03-05" });
    const article = news({
      published_date: "2026-03-04",
      funding_round_id: "r-gone", // stale link — not among the passed rounds
    });
    const items = buildTimeline([r], [article]);
    const live = items.find((i) => i.kind === "funding" && i.round.id === "r-live");
    expect(
      live?.kind === "funding" ? live.coverage.map((c) => c.url) : [],
    ).toContain(article.url);
    expect(items.filter((i) => i.kind === "news")).toHaveLength(0);
  });
});

describe("standalone story clustering (the kalshi/blue-origin firehose)", () => {
  const NO_ROUNDS: FundingRoundWithInvestors[] = [];

  it("collapses the same syndicated headline into one story with coverage", () => {
    // The blue-origin shape: one piece re-served by four outlets, titles
    // differing only in the trailing "- Outlet" segment.
    const articles = [
      news({
        title: "Jeff Bezos Put $2 Billion of His Own Money Into Blue Origin's Funding Round - MSN",
        url: "https://news.google.com/rss/articles/a1",
        source: "news.google.com",
        published_date: "2026-07-15",
      }),
      news({
        title: "Jeff Bezos Put $2 Billion of His Own Money Into Blue Origin's Funding Round - AOL.com",
        url: "https://news.google.com/rss/articles/a2",
        source: "news.google.com",
        published_date: "2026-07-14",
      }),
      news({
        title: "Jeff Bezos Put $2 Billion of His Own Money Into Blue Origin's Funding Round - The Motley Fool",
        url: "https://news.google.com/rss/articles/a3",
        source: "news.google.com",
        published_date: "2026-07-14",
      }),
      news({
        title: "Jeff Bezos put $2 billion of his own money into Blue Origin funding round - Yahoo Finance",
        url: "https://news.google.com/rss/articles/a4",
        source: "news.google.com",
        published_date: "2026-07-14",
      }),
    ];
    const items = standaloneNews(NO_ROUNDS, articles);
    expect(items).toHaveLength(1);
    const story = items[0];
    if (story.kind !== "news") throw new Error("expected news");
    expect(story.coverage).toHaveLength(4);
    // Lead = newest article; it renders the row.
    expect(story.article.published_date).toBe("2026-07-15");
    expect(story.coverage[0].url).toBe(story.article.url);
  });

  it("keeps DIFFERENT stories apart even in the same week", () => {
    // "seeks $10B" (rumor era) vs "$2B of his own money" — different events.
    const seeks = news({
      title: "Blue Origin seeks $10bn in funding - TechCentral.ie",
      url: "https://news.google.com/rss/articles/s1",
      published_date: "2026-07-13",
    });
    const seeks2 = news({
      title: "Jeff Bezos' Blue Origin Seeks $10 Billion in First Outside Funding - Tempo.co",
      url: "https://news.google.com/rss/articles/s2",
      published_date: "2026-07-13",
    });
    const ownMoney = news({
      title: "Jeff Bezos Put $2 Billion of His Own Money Into Blue Origin's Funding Round - MSN",
      url: "https://news.google.com/rss/articles/o1",
      published_date: "2026-07-15",
    });
    const items = standaloneNews(NO_ROUNDS, [seeks, seeks2, ownMoney]);
    expect(items).toHaveLength(2);
    const sizes = items
      .map((i) => (i.kind === "news" ? i.coverage.length : 0))
      .sort();
    expect(sizes).toEqual([1, 2]); // the two "seeks" variants merged
  });

  it("folds money spellings and announce verbs into one story", () => {
    const a = news({
      title: "Kalshi raises $300M at a $5B valuation - TechCrunch",
      url: "https://techcrunch.com/kalshi",
      published_date: "2026-07-10",
    });
    const b = news({
      title: "Kalshi raised $300 million at $5 billion valuation - Reuters",
      url: "https://reuters.com/kalshi",
      published_date: "2026-07-11",
    });
    const items = standaloneNews(NO_ROUNDS, [a, b]);
    expect(items).toHaveLength(1);
  });

  it("does not merge beyond the story window", () => {
    const a = news({
      title: "Acme raises $50M Series B - TechCrunch",
      url: "https://techcrunch.com/acme-1",
      published_date: "2026-07-01",
    });
    const b = news({
      title: "Acme raises $50M Series B - Reuters",
      url: "https://reuters.com/acme-2",
      published_date: "2026-07-12", // 11 days later, beyond STORY_WINDOW_DAYS
    });
    expect(standaloneNews(NO_ROUNDS, [a, b])).toHaveLength(2);
  });

  it("never merges undated articles and keeps singletons as plain rows", () => {
    const a = news({
      title: "Acme raises $50M - TechCrunch",
      url: "https://techcrunch.com/u1",
      published_date: null,
    });
    const b = news({
      title: "Acme raises $50M - Reuters",
      url: "https://reuters.com/u2",
      published_date: null,
    });
    const items = standaloneNews(NO_ROUNDS, [a, b]);
    expect(items).toHaveLength(2);
    for (const i of items) {
      if (i.kind !== "news") throw new Error("expected news");
      expect(i.coverage).toHaveLength(1);
    }
  });

  it("round-attached articles never leak into story clusters", () => {
    // An article that attaches to a round (in-window) must not ALSO seed a
    // story cluster; a far-away syndicated pair still clusters on its own.
    const r = round({ announced_date: "2026-07-10" });
    const attached = news({
      title: "Acme raises $50M Series B - TechCrunch",
      url: "https://techcrunch.com/attached",
      published_date: "2026-07-11",
    });
    const stray1 = news({
      title: "Acme eyes IPO next year, sources say - Forbes",
      url: "https://forbes.com/stray-1",
      published_date: "2026-08-20", // far outside the round's window
    });
    const stray2 = news({
      title: "Acme eyes IPO next year, sources say - MSN",
      url: "https://news.google.com/rss/articles/stray-2",
      published_date: "2026-08-21",
    });
    const items = buildTimeline([r], [attached, stray1, stray2]);
    const newsItems = items.filter((i) => i.kind === "news");
    expect(newsItems).toHaveLength(1); // one story cluster, not two rows
    if (newsItems[0].kind === "news") {
      expect(newsItems[0].coverage).toHaveLength(2);
      expect(
        newsItems[0].coverage.map((c) => c.url),
      ).not.toContain(attached.url);
    }
    const funding = items.find((i) => i.kind === "funding");
    if (funding?.kind === "funding") {
      expect(funding.coverage.map((c) => c.url)).toContain(attached.url);
    }
  });
});
