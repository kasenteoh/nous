// /industry/[group]/feed.xml — recent funding + news for companies in one
// canonical industry_group, newest-first. A per-entity fan-out of the global
// /feed.xml firehose. Mirrors app/feed.xml/route.ts (revalidate, cached RSS
// Response, empty-but-valid on missing Supabase). The slug is hard-gated to a
// canonical bucket via resolveIndustrySlug — exactly the /industry/[group] page
// gate — so a freeform label can never mint a feed; a non-canonical slug 404s.

import { buildRssFeed } from "@/lib/rss";
import {
  fundingToRssItem,
  mergeFeedItems,
  newsToRssItem,
  rssResponse,
} from "@/lib/rss-items";
import {
  listCanonicalIndustries,
  listRecentFundingsByIndustry,
  listRecentNewsByIndustry,
} from "@/lib/queries";
import { isSupabaseConfigured } from "@/lib/db";
import { industryToSlug, resolveIndustrySlug } from "@/lib/industry";
import { siteOrigin } from "@/lib/site";

// Regenerate at most every 6 hours, matching the pages' ISR window.
export const revalidate = 21600;

const PER_SOURCE = 30;
const FEED_SIZE = 40;

type RouteContext = { params: Promise<{ group: string }> };

export async function GET(
  _req: Request,
  { params }: RouteContext,
): Promise<Response> {
  const { group: slug } = await params;
  const origin = siteOrigin();

  // Supabase intentionally absent (secret-free CI/local build): degrade to an
  // empty-but-valid feed rather than 404/500. When configured, the slug is
  // gated to a canonical industry below; a non-canonical slug 404s.
  if (!isSupabaseConfigured()) {
    return rssResponse(emptyIndustryFeed(origin, slug));
  }

  const industries = await listCanonicalIndustries();
  const group = resolveIndustrySlug(
    slug,
    industries.map((i) => i.group),
  );
  if (!group) {
    return new Response("Not found", { status: 404 });
  }

  const [fundings, news] = await Promise.all([
    listRecentFundingsByIndustry(group, PER_SOURCE),
    listRecentNewsByIndustry(group, PER_SOURCE),
  ]);

  const items = mergeFeedItems(
    [
      ...fundings.map((f) => fundingToRssItem(f, origin)),
      ...news.map((n) => newsToRssItem(n)),
    ],
    FEED_SIZE,
  );

  const canonicalSlug = industryToSlug(group);
  const xml = buildRssFeed({
    title: `${group} — funding & news on nous`,
    link: `${origin}/industry/${canonicalSlug}`,
    feedUrl: `${origin}/industry/${canonicalSlug}/feed.xml`,
    description: `The latest funding rounds and news across ${group} startups tracked by nous.`,
    items,
  });

  return rssResponse(xml);
}

/** Empty-but-valid feed used only on the missing-Supabase degradation path. */
function emptyIndustryFeed(origin: string, slug: string): string {
  return buildRssFeed({
    title: "nous industry feed",
    link: `${origin}/industry/${slug}`,
    feedUrl: `${origin}/industry/${slug}/feed.xml`,
    description: "Funding rounds and news for this industry, tracked by nous.",
    items: [],
  });
}
