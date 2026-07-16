// /robots.txt — open to all crawlers except /surprise, which is a
// force-dynamic random redirect (a crawler trap: every hit is a new "page"
// and burns crawl budget on duplicate content).
//
// Sitemaps are SHARDED (lib/sitemap-shards.ts): Next.js generateSitemaps
// serves /sitemap/core.xml + /sitemap/companies-<i>.xml but writes no index
// file, so every shard is listed here — crawlers accept multiple Sitemap:
// lines. Re-generated on the same 6h ISR window as the sitemaps, so a new
// company shard is advertised in the same revalidation cycle it starts
// existing.

export const revalidate = 21600;

import type { MetadataRoute } from "next";
import { siteOrigin } from "@/lib/site";
import { sitemapIds } from "@/lib/sitemap-shards";

export default async function robots(): Promise<MetadataRoute.Robots> {
  const origin = siteOrigin();
  const ids = await sitemapIds();
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      disallow: ["/surprise"],
    },
    sitemap: ids.map((id) => `${origin}/sitemap/${id}.xml`),
  };
}
