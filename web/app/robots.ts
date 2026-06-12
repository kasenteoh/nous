// /robots.txt — open to all crawlers except /surprise, which is a
// force-dynamic random redirect (a crawler trap: every hit is a new "page"
// and burns crawl budget on duplicate content).

import type { MetadataRoute } from "next";
import { siteOrigin } from "@/lib/site";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      disallow: ["/surprise"],
    },
    sitemap: `${siteOrigin()}/sitemap.xml`,
  };
}
