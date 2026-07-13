import { describe, expect, it } from "vitest";

import { buildRssFeed, toRfc822, xmlEscape } from "@/lib/rss";

describe("xmlEscape", () => {
  it("escapes the five XML-significant characters", () => {
    expect(xmlEscape(`Tom & Jerry <"'>`)).toBe(
      "Tom &amp; Jerry &lt;&quot;&apos;&gt;",
    );
  });

  it("escapes ampersands before other entities (no double-escaping)", () => {
    expect(xmlEscape("A&B<C")).toBe("A&amp;B&lt;C");
  });
});

describe("toRfc822", () => {
  it("formats a date-only string as RFC-822 UTC midnight", () => {
    expect(toRfc822("2026-05-12")).toBe("Tue, 12 May 2026 00:00:00 GMT");
  });

  it("returns null for null or unparseable input (caller omits pubDate)", () => {
    expect(toRfc822(null)).toBeNull();
    expect(toRfc822("not-a-date")).toBeNull();
  });
});

describe("buildRssFeed", () => {
  const feed = buildRssFeed({
    title: "nous feed",
    link: "https://x.test",
    feedUrl: "https://x.test/feed.xml",
    description: "desc",
    items: [
      {
        title: "Acme raised $10M & grew",
        link: "https://x.test/c/acme",
        description: "big news",
        guid: "funding:acme:2026-05-01:10000000",
        pubDate: "2026-05-01",
      },
      {
        title: "Undated item",
        link: "https://x.test/c/globex",
        description: "no date",
        guid: "news:42",
        pubDate: null,
      },
    ],
  });

  it("emits a valid RSS 2.0 envelope with the atom self link", () => {
    expect(feed).toContain('<?xml version="1.0" encoding="UTF-8"?>');
    expect(feed).toContain('<rss version="2.0"');
    expect(feed).toContain(
      '<atom:link href="https://x.test/feed.xml" rel="self" type="application/rss+xml" />',
    );
  });

  it("escapes item content", () => {
    expect(feed).toContain(
      "<title>Acme raised $10M &amp; grew</title>",
    );
  });

  it("emits <pubDate> for dated items and omits it for undated ones", () => {
    expect(feed).toContain("<pubDate>Fri, 01 May 2026 00:00:00 GMT</pubDate>");
    // The undated item's block carries a guid but no pubDate line.
    expect(feed).toContain('<guid isPermaLink="false">news:42</guid>');
    const undatedBlock = feed.slice(feed.indexOf("news:42"));
    expect(undatedBlock).not.toContain("<pubDate>");
  });
});
