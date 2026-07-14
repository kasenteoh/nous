// /feed.xml — an RSS 2.0 firehose of the catalog's newest events: funding
// rounds (from recorded rounds) and news articles, interleaved newest-first.
// Read-only, on-site distribution only (email is out this quarter). Route
// handler rather than a page: RSS is XML, not HTML. Degrades to an empty but
// valid feed when Supabase is absent (CI build) — never 500s.
//
// The per-entity feeds (company / industry / investor) mirror this structure;
// the row → RssItem mapping, newest-first merge, and cached Response all live in
// lib/rss-items.ts so every feed emits an identical item shape.

import { listRecentFundings, listRecentNews } from "@/lib/queries";
import { buildRssFeed } from "@/lib/rss";
import {
  fundingToRssItem,
  mergeFeedItems,
  newsToRssItem,
  rssResponse,
} from "@/lib/rss-items";
import { siteOrigin } from "@/lib/site";

// Regenerate at most every 6 hours, matching the pages' ISR window.
export const revalidate = 21600;

// How many of each event type to pull before merging + trimming.
const PER_SOURCE = 30;
const FEED_SIZE = 40;

export async function GET(): Promise<Response> {
  const origin = siteOrigin();

  const [fundings, news] = await Promise.all([
    listRecentFundings(PER_SOURCE),
    listRecentNews(PER_SOURCE),
  ]);

  // Merge and sort newest-first; both sources are already date-filtered.
  const items = mergeFeedItems(
    [
      ...fundings.map((f) => fundingToRssItem(f, origin)),
      ...news.map((n) => newsToRssItem(n)),
    ],
    FEED_SIZE,
  );

  const xml = buildRssFeed({
    title: "nous — new US software startup funding & news",
    link: origin,
    feedUrl: `${origin}/feed.xml`,
    description:
      "The newest funding rounds and news across nous's directory of US " +
      "software startups.",
    items,
  });

  return rssResponse(xml);
}
