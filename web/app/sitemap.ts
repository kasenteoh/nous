// /sitemap.xml — static routes + one entry per company page. When Supabase
// env is absent (CI builds without secrets), listAllCompanySlugs returns []
// and the sitemap still builds with just the static entries.

// Re-generate at most every 6 hours, matching the pages' ISR window.
export const revalidate = 21600;

import type { MetadataRoute } from "next";
import {
  listAllCompanySlugs,
  listAllInvestorSlugs,
  listAllTags,
  listAllStates,
  listAlternativesCompanySlugs,
} from "@/lib/queries";
import { siteOrigin } from "@/lib/site";

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const origin = siteOrigin();

  const staticEntries: MetadataRoute.Sitemap = [
    { url: `${origin}/` },
    { url: `${origin}/companies` },
    { url: `${origin}/investors` },
    { url: `${origin}/new` },
    { url: `${origin}/about` },
  ];

  const [companies, investors, tags, states, alternatives] = await Promise.all([
    listAllCompanySlugs(),
    listAllInvestorSlugs(),
    listAllTags(),
    listAllStates(),
    listAlternativesCompanySlugs(),
  ]);

  const companyEntries: MetadataRoute.Sitemap = companies.map((c) => ({
    url: `${origin}/c/${c.slug}`,
    lastModified: c.updated_at ?? undefined,
  }));

  const investorEntries: MetadataRoute.Sitemap = investors.map((i) => ({
    url: `${origin}/investor/${i.slug}`,
    lastModified: i.updated_at ?? undefined,
  }));

  // `tags` is already the de-thinned list — listAllTags returns only tags
  // applying to ≥ MIN_TAG_COMPANY_COUNT companies — so the sitemap is no longer
  // dominated by one-company tag pages. No further filtering needed here.
  const tagEntries: MetadataRoute.Sitemap = tags.map((tag) => ({
    url: `${origin}/tag/${encodeURIComponent(tag)}`,
  }));

  const locationEntries: MetadataRoute.Sitemap = states.map((state) => ({
    url: `${origin}/location/${encodeURIComponent(state)}`,
  }));

  // `alternatives` is already the de-thinned list — listAlternativesCompanySlugs
  // returns only companies with ≥ MIN_ALTERNATIVES_COMPETITOR_COUNT competitor
  // rows (mirroring the tag threshold) — so thin one/two-competitor pages stay
  // out of the sitemap. No further filtering needed here.
  const alternativesEntries: MetadataRoute.Sitemap = alternatives.map((c) => ({
    url: `${origin}/alternatives/${c.slug}`,
    lastModified: c.updated_at ?? undefined,
  }));

  return [
    ...staticEntries,
    ...companyEntries,
    ...investorEntries,
    ...tagEntries,
    ...locationEntries,
    ...alternativesEntries,
  ];
}
