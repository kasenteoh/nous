import { describe, expect, it } from "vitest";

import {
  fundingToRssItem,
  mergeFeedItems,
  newsToRssItem,
  type FeedFundingRow,
  type FeedNewsRow,
} from "@/lib/rss-items";
import type { RssItem } from "@/lib/rss";

const ORIGIN = "https://nous.test";

describe("fundingToRssItem", () => {
  const base: FeedFundingRow = {
    companySlug: "acme",
    companyName: "Acme",
    round_type: "Series A",
    amount_raised: 10_000_000,
    announced_date: "2026-05-01",
  };

  it("titles a dated, amounted round with the round type and links to the nous page", () => {
    const item = fundingToRssItem(base, ORIGIN);
    expect(item.title).toBe("Acme raised $10M (Series A)");
    expect(item.link).toBe("https://nous.test/c/acme");
    expect(item.description).toBe(
      "Acme raised $10M (Series A), announced 2026-05-01.",
    );
    expect(item.pubDate).toBe("2026-05-01");
  });

  it("uses a stable guid keyed on slug + date + amount, mirroring the global feed", () => {
    expect(fundingToRssItem(base, ORIGIN).guid).toBe(
      "funding:acme:2026-05-01:10000000",
    );
    // Same round → same guid on any regeneration (and in any feed it appears in).
    expect(fundingToRssItem(base, ORIGIN).guid).toBe(
      fundingToRssItem(base, ORIGIN).guid,
    );
  });

  it("falls back to a generic title when the amount is unknown or zero, and marks the guid 'na'", () => {
    const noAmount = fundingToRssItem(
      { ...base, amount_raised: null },
      ORIGIN,
    );
    expect(noAmount.title).toBe("Acme — new funding round (Series A)");
    expect(noAmount.guid).toBe("funding:acme:2026-05-01:na");

    const zero = fundingToRssItem({ ...base, amount_raised: 0 }, ORIGIN);
    expect(zero.title).toBe("Acme — new funding round (Series A)");
    // Zero still records an amount in the guid (distinct from unknown "na").
    expect(zero.guid).toBe("funding:acme:2026-05-01:0");
  });

  it("omits the round-type suffix when the round type is null", () => {
    const item = fundingToRssItem({ ...base, round_type: null }, ORIGIN);
    expect(item.title).toBe("Acme raised $10M");
  });
});

describe("newsToRssItem", () => {
  const base: FeedNewsRow = {
    id: "n-42",
    title: "Acme launches widget",
    url: "https://news.test/acme-widget",
    source: "TechCrunch",
    companyName: "Acme",
    published_date: "2026-04-15",
  };

  it("links to the original article and keys the guid on the article id", () => {
    const item = newsToRssItem(base);
    expect(item.title).toBe("Acme launches widget");
    expect(item.link).toBe("https://news.test/acme-widget");
    expect(item.guid).toBe("news:n-42");
    expect(item.description).toBe("Acme in the news — TechCrunch.");
    expect(item.pubDate).toBe("2026-04-15");
  });

  it("drops the source suffix when the source is empty", () => {
    expect(newsToRssItem({ ...base, source: "" }).description).toBe(
      "Acme in the news.",
    );
  });
});

describe("mergeFeedItems", () => {
  const item = (guid: string, pubDate: string | null): RssItem => ({
    title: guid,
    link: `https://nous.test/${guid}`,
    description: guid,
    guid,
    pubDate,
  });

  it("sorts newest-first with undated items last, capped to size", () => {
    const merged = mergeFeedItems(
      [
        item("a", "2026-01-01"),
        item("b", null),
        item("c", "2026-06-01"),
        item("d", "2026-03-01"),
      ],
      3,
    );
    expect(merged.map((i) => i.guid)).toEqual(["c", "d", "a"]);
  });

  it("does not mutate the input array", () => {
    const input = [item("a", "2026-01-01"), item("c", "2026-06-01")];
    const snapshot = input.map((i) => i.guid);
    mergeFeedItems(input, 10);
    expect(input.map((i) => i.guid)).toEqual(snapshot);
  });
});
