// /sitemap.xml — static routes + one entry per company page. When Supabase
// env is absent (CI builds without secrets), listAllCompanySlugs returns []
// and the sitemap still builds with just the static entries.

// Re-generate at most every 6 hours, matching the pages' ISR window.
export const revalidate = 21600;

import type { MetadataRoute } from "next";
import {
  listAllCompanySlugs,
  listAllTags,
  listAllStates,
} from "@/lib/queries";
import { siteOrigin } from "@/lib/site";

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const origin = siteOrigin();

  const staticEntries: MetadataRoute.Sitemap = [
    { url: `${origin}/` },
    { url: `${origin}/companies` },
    { url: `${origin}/new` },
    { url: `${origin}/about` },
  ];

  const [companies, tags, states] = await Promise.all([
    listAllCompanySlugs(),
    listAllTags(),
    listAllStates(),
  ]);

  const companyEntries: MetadataRoute.Sitemap = companies.map((c) => ({
    url: `${origin}/c/${c.slug}`,
    lastModified: c.updated_at ?? undefined,
  }));

  const tagEntries: MetadataRoute.Sitemap = tags.map((tag) => ({
    url: `${origin}/tag/${encodeURIComponent(tag)}`,
  }));

  const locationEntries: MetadataRoute.Sitemap = states.map((state) => ({
    url: `${origin}/location/${encodeURIComponent(state)}`,
  }));

  return [...staticEntries, ...companyEntries, ...tagEntries, ...locationEntries];
}
