// Glue between DB event rows and the pure `buildRssFeed` builder (lib/rss.ts):
// maps a funding row / news row to an RssItem with the site's stable guid
// scheme, merges the two streams newest-first, and wraps a built document in
// the shared cached RSS Response. Shared by the global /feed.xml firehose and
// the per-entity feeds (company / industry / investor) so every feed emits an
// identical item shape and one stable identity per event.
//
// Kept DB-free (no `server-only`, no Supabase) so it stays unit-testable and
// importable from any route handler. `formatUsd` is pure, dependency-free.

import { formatUsd } from "@/lib/format";
import type { RssItem } from "@/lib/rss";

/**
 * The funding fields a feed item needs. Deliberately narrower than
 * `RecentFundingRow` (structurally compatible with it) so callers that build
 * this from a company's timeline rounds can supply the same shape.
 * `announced_date` is non-null: the guid and <pubDate> both key on it, so
 * callers filter undated rounds out before mapping (matching the global feed).
 */
export interface FeedFundingRow {
  companySlug: string;
  companyName: string;
  round_type: string | null;
  amount_raised: number | null;
  announced_date: string;
}

/**
 * The news fields a feed item needs. Structurally compatible with
 * `RecentNewsRow` (extra fields on that type are ignored). `published_date` may
 * be null — the builder simply omits <pubDate> for undated items.
 */
export interface FeedNewsRow {
  id: string;
  title: string;
  url: string;
  source: string;
  companyName: string;
  published_date: string | null;
}

/**
 * Map a funding round to an RssItem. The guid is stable across regenerations —
 * same round → same guid — and identical to the global firehose's scheme, so a
 * reader subscribed to several feeds dedupes one event once. Links to the
 * company's nous page (the round's canonical home on-site).
 */
export function fundingToRssItem(f: FeedFundingRow, origin: string): RssItem {
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
}

/**
 * Map a news article to an RssItem. News items link to the original article
 * (not a nous page); the guid keys on the article id, globally unique and
 * stable. No `origin` needed — the link is the source URL.
 */
export function newsToRssItem(n: FeedNewsRow): RssItem {
  return {
    title: n.title,
    link: n.url,
    description: `${n.companyName} in the news${n.source ? ` — ${n.source}` : ""}.`,
    guid: `news:${n.id}`,
    pubDate: n.published_date,
  };
}

/**
 * Merge already-mapped funding + news items into one stream, newest-first,
 * trimmed to `size`. Undated items (null pubDate) sort last. Pure — sorts a
 * copy, never mutates the input.
 */
export function mergeFeedItems(items: RssItem[], size: number): RssItem[] {
  return [...items]
    .sort((a, b) => (b.pubDate ?? "").localeCompare(a.pubDate ?? ""))
    .slice(0, size);
}

/**
 * How long a feed may be cached — 6 hours, matching every page's ISR window and
 * the `export const revalidate` each feed route declares. Kept here so the
 * Cache-Control s-maxage and the routes' revalidate can't silently drift; note
 * Next requires the route's `revalidate` export to be a literal, so each route
 * still writes `21600` inline and only the header reads this constant.
 */
export const FEED_REVALIDATE_SECONDS = 21600;

/** Wrap a built RSS document in the shared, CDN-cacheable RSS response. */
export function rssResponse(xml: string): Response {
  return new Response(xml, {
    headers: {
      "content-type": "application/rss+xml; charset=utf-8",
      // Let CDNs cache it in step with the ISR window.
      "cache-control": `public, max-age=0, s-maxage=${FEED_REVALIDATE_SECONDS}`,
    },
  });
}
