// /investor/[slug]/feed.xml — recent funding + news for an investor's portfolio
// companies, newest-first. A per-entity fan-out of the global /feed.xml
// firehose. Mirrors app/feed.xml/route.ts (revalidate, cached RSS Response,
// empty-but-valid on missing Supabase). Resolves the investor via
// getInvestorBySlug (reusing its portfolio union — company-level links AND
// round-only companies, excluded companies already dropped); a configured-but-
// unknown investor 404s.

import { buildRssFeed } from "@/lib/rss";
import {
  FEED_IN_SLUGS_CAP,
  getInvestorBySlug,
  listRecentFundingsForCompanySlugs,
  listRecentNewsForCompanySlugs,
} from "@/lib/queries";
import {
  fundingToRssItem,
  mergeFeedItems,
  newsToRssItem,
  rssResponse,
} from "@/lib/rss-items";
import { isSupabaseConfigured } from "@/lib/db";
import { siteOrigin } from "@/lib/site";

// Regenerate at most every 6 hours, matching the pages' ISR window.
export const revalidate = 21600;

const PER_SOURCE = 30;
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
  // unknown investor falls through to a truthful 404 below.
  if (!isSupabaseConfigured()) {
    return rssResponse(emptyInvestorFeed(origin, slug));
  }

  // No opts → the full resolved portfolio union (both link paths, excluded
  // companies already dropped).
  const investor = await getInvestorBySlug(slug);
  if (!investor) {
    return new Response("Not found", { status: 404 });
  }

  // Cap the slug set so the `.in(...)` filter can't overrun the request URL
  // length on the largest portfolios; the feed still surfaces the 40 most
  // recent events across the first N companies (portfolio is name-sorted).
  const slugs = investor.portfolio
    .map((c) => c.slug)
    .slice(0, FEED_IN_SLUGS_CAP);

  const [fundings, news] = await Promise.all([
    listRecentFundingsForCompanySlugs(slugs, PER_SOURCE),
    listRecentNewsForCompanySlugs(slugs, PER_SOURCE),
  ]);

  const items = mergeFeedItems(
    [
      ...fundings.map((f) => fundingToRssItem(f, origin)),
      ...news.map((n) => newsToRssItem(n)),
    ],
    FEED_SIZE,
  );

  const xml = buildRssFeed({
    title: `${investor.name} portfolio — funding & news on nous`,
    link: `${origin}/investor/${investor.slug}`,
    feedUrl: `${origin}/investor/${investor.slug}/feed.xml`,
    description: `The latest funding rounds and news across ${investor.name}'s portfolio companies, tracked by nous.`,
    items,
  });

  return rssResponse(xml);
}

/** Empty-but-valid feed used only on the missing-Supabase degradation path. */
function emptyInvestorFeed(origin: string, slug: string): string {
  return buildRssFeed({
    title: "nous investor feed",
    link: `${origin}/investor/${slug}`,
    feedUrl: `${origin}/investor/${slug}/feed.xml`,
    description:
      "Funding rounds and news across this investor's portfolio, tracked by nous.",
    items: [],
  });
}
