// /feed.xml — an RSS 2.0 firehose of the catalog's newest events: funding
// rounds (from recorded rounds) and news articles, interleaved newest-first.
// Read-only, on-site distribution only (email is out this quarter). Route
// handler rather than a page: RSS is XML, not HTML. Degrades to an empty but
// valid feed when Supabase is absent (CI build) — never 500s.

import { listRecentFundings, listRecentNews } from "@/lib/queries";
import { buildRssFeed, type RssItem } from "@/lib/rss";
import { formatUsd } from "@/lib/format";
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

  const fundingItems: RssItem[] = fundings.map((f) => {
    const amount =
      f.amount_raised != null && f.amount_raised > 0
        ? formatUsd(f.amount_raised)
        : null;
    const round = f.round_type ? ` (${f.round_type})` : "";
    const title = amount
      ? `${f.companyName} raised ${amount}${round}`
      : `${f.companyName} — new funding round${round}`;
    return {
      title,
      link: `${origin}/c/${f.companySlug}`,
      description: `${title}, announced ${f.announced_date}.`,
      // Stable across regenerations: same round → same guid.
      guid: `funding:${f.companySlug}:${f.announced_date}:${f.amount_raised ?? "na"}`,
      pubDate: f.announced_date,
    };
  });

  const newsItems: RssItem[] = news.map((n) => ({
    title: n.title,
    // News items link to the original article, not the nous page.
    link: n.url,
    description: `${n.companyName} in the news${n.source ? ` — ${n.source}` : ""}.`,
    guid: `news:${n.id}`,
    pubDate: n.published_date,
  }));

  // Merge and sort newest-first; both sources are already date-filtered.
  const items = [...fundingItems, ...newsItems]
    .sort((a, b) => (b.pubDate ?? "").localeCompare(a.pubDate ?? ""))
    .slice(0, FEED_SIZE);

  const xml = buildRssFeed({
    title: "nous — new US software startup funding & news",
    link: origin,
    feedUrl: `${origin}/feed.xml`,
    description:
      "The newest funding rounds and news across nous's directory of US " +
      "software startups.",
    items,
  });

  return new Response(xml, {
    headers: {
      "content-type": "application/rss+xml; charset=utf-8",
      // Let CDNs cache it in step with the ISR window.
      "cache-control": "public, max-age=0, s-maxage=21600",
    },
  });
}
