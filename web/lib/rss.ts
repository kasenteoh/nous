// Pure RSS 2.0 feed builder for /feed.xml. Kept DB-free and side-effect-free so
// the XML assembly + escaping are unit-testable; the route handler fetches the
// events, maps them to RssItem[], and calls buildRssFeed. No `server-only` —
// nothing here touches secrets.

export interface RssItem {
  /** Plain-text title (escaped here). */
  title: string;
  /** Absolute URL. */
  link: string;
  /** Plain-text summary (escaped here). */
  description: string;
  /** Stable unique id for the item (a URL or synthetic key). */
  guid: string;
  /** ISO date/timestamp, or null when undated (the <pubDate> is then omitted). */
  pubDate: string | null;
}

/** Escape the five XML-significant characters for text/CDATA-free content. */
export function xmlEscape(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

/**
 * Format an ISO date (YYYY-MM-DD or full timestamp) as an RFC-822 date for
 * <pubDate>, in UTC. Returns null for a null/unparseable input so the caller
 * omits the element rather than emitting a bogus date. Date-only strings are
 * pinned to 00:00:00Z (the announce/publish granularity we store is the day).
 */
export function toRfc822(iso: string | null): string | null {
  if (!iso) return null;
  const date = new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toUTCString();
}

interface FeedOptions {
  title: string;
  /** The site URL the feed describes. */
  link: string;
  /** Absolute URL of the feed document itself (for atom:self). */
  feedUrl: string;
  description: string;
  items: RssItem[];
}

/** Assemble a valid RSS 2.0 document. All text is XML-escaped here. */
export function buildRssFeed(opts: FeedOptions): string {
  const itemsXml = opts.items
    .map((item) => {
      const pub = toRfc822(item.pubDate);
      return [
        "    <item>",
        `      <title>${xmlEscape(item.title)}</title>`,
        `      <link>${xmlEscape(item.link)}</link>`,
        `      <guid isPermaLink="false">${xmlEscape(item.guid)}</guid>`,
        pub ? `      <pubDate>${pub}</pubDate>` : null,
        `      <description>${xmlEscape(item.description)}</description>`,
        "    </item>",
      ]
        .filter((line): line is string => line !== null)
        .join("\n");
    })
    .join("\n");

  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
    "  <channel>",
    `    <title>${xmlEscape(opts.title)}</title>`,
    `    <link>${xmlEscape(opts.link)}</link>`,
    `    <description>${xmlEscape(opts.description)}</description>`,
    `    <atom:link href="${xmlEscape(opts.feedUrl)}" rel="self" type="application/rss+xml" />`,
    itemsXml,
    "  </channel>",
    "</rss>",
    "",
  ].join("\n");
}
