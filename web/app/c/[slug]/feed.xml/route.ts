// /c/[slug]/feed.xml — one company's event feed: its funding rounds + news,
// newest-first. A per-entity fan-out of the global /feed.xml firehose so a
// reader can "watch this company" in any feed reader, no account needed.
// Mirrors app/feed.xml/route.ts (revalidate, cached RSS Response, empty-but-
// valid on missing Supabase). Reuses the /c/[slug] timeline query
// (getCompanyBySlug already returns funding rounds + news for one company)
// rather than adding a new one.

import { buildRssFeed } from "@/lib/rss";
import {
  fundingToRssItem,
  mergeFeedItems,
  newsToRssItem,
  rssResponse,
} from "@/lib/rss-items";
import { getCompanyBySlug } from "@/lib/queries";
import { isSupabaseConfigured } from "@/lib/db";
import { siteOrigin } from "@/lib/site";
import type { FundingRoundWithInvestors, NewsArticleRow } from "@/lib/types";

// Regenerate at most every 6 hours, matching the pages' ISR window.
export const revalidate = 21600;

const FEED_SIZE = 40;

type RouteContext = { params: Promise<{ slug: string }> };

export async function GET(
  _req: Request,
  { params }: RouteContext,
): Promise<Response> {
  const { slug } = await params;
  const origin = siteOrigin();

  // Supabase intentionally absent (secret-free CI/local build): degrade to an
  // empty-but-valid feed for the slug rather than 404/500. A configured-but-
  // unknown company falls through to a truthful 404 below.
  if (!isSupabaseConfigured()) {
    return rssResponse(emptyCompanyFeed(origin, slug));
  }

  const detail = await getCompanyBySlug(slug);
  if (!detail) {
    return new Response("Not found", { status: 404 });
  }

  const { company, fundingRounds, news } = detail;

  // Dated-only, matching the global firehose: the guid + <pubDate> key on the
  // date, and undated events don't sort meaningfully in a reverse-chron feed.
  const fundingItems = fundingRounds
    .filter(
      (r): r is FundingRoundWithInvestors & { announced_date: string } =>
        r.announced_date != null,
    )
    .map((r) =>
      fundingToRssItem(
        {
          companySlug: company.slug,
          companyName: company.name,
          round_type: r.round_type,
          amount_raised: r.amount_raised,
          announced_date: r.announced_date,
        },
        origin,
      ),
    );

  const newsItems = news
    .filter(
      (n): n is NewsArticleRow & { published_date: string } =>
        n.published_date != null,
    )
    .map((n) =>
      newsToRssItem({
        id: n.id,
        title: n.title,
        url: n.url,
        source: n.source,
        companyName: company.name,
        published_date: n.published_date,
      }),
    );

  const items = mergeFeedItems([...fundingItems, ...newsItems], FEED_SIZE);

  const xml = buildRssFeed({
    title: `${company.name} — funding & news on nous`,
    link: `${origin}/c/${company.slug}`,
    feedUrl: `${origin}/c/${company.slug}/feed.xml`,
    description: `The latest funding rounds and news for ${company.name}, tracked by nous.`,
    items,
  });

  return rssResponse(xml);
}

/** Empty-but-valid feed used only on the missing-Supabase degradation path. */
function emptyCompanyFeed(origin: string, slug: string): string {
  return buildRssFeed({
    title: "nous company feed",
    link: `${origin}/c/${slug}`,
    feedUrl: `${origin}/c/${slug}/feed.xml`,
    description: "Funding rounds and news for this company, tracked by nous.",
    items: [],
  });
}
