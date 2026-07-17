// Sharded sitemaps at /sitemap/[id].xml (see lib/sitemap-shards.ts): "core"
// carries the static routes + every non-company entity; "companies-<i>" shards
// carry company pages in stable slug order, COMPANY_SHARD_SIZE per file, so
// the catalog can grow past Google's 50k-URLs-per-file cap without a rework.
// Next.js emits no sitemap index — app/robots.ts lists every shard URL.
// When Supabase env is absent (CI builds without secrets), the queries return
// [] and every shard still builds (core = static entries; companies-0 empty).

// Re-generate at most every 6 hours, matching the pages' ISR window.
export const revalidate = 21600;

import type { MetadataRoute } from "next";
import {
  listAllCompanySlugs,
  listAllInvestorSlugs,
  listAllTags,
  listAllStates,
  listAllThemeSlugs,
  listAlternativesCompanySlugs,
  listCanonicalIndustries,
  listIndustriesWithMapCoords,
} from "@/lib/queries";
import { industryToSlug } from "@/lib/industry";
import { siteOrigin } from "@/lib/site";
import {
  COMPANY_SHARD_SIZE,
  companyShardIndex,
  sitemapIds,
} from "@/lib/sitemap-shards";

export async function generateSitemaps(): Promise<{ id: string }[]> {
  return (await sitemapIds()).map((id) => ({ id }));
}

export default async function sitemap(props: {
  id: Promise<string>;
}): Promise<MetadataRoute.Sitemap> {
  const id = await props.id;
  const origin = siteOrigin();

  const shardIndex = companyShardIndex(id);
  if (shardIndex !== null) {
    // Company shard: stable slug order (scanCompanies orders by slug), sliced
    // by shard index. A shard past the end (count shrank between
    // generateSitemaps and now) renders empty-but-valid, never a 404.
    const companies = await listAllCompanySlugs();
    const start = shardIndex * COMPANY_SHARD_SIZE;
    return companies.slice(start, start + COMPANY_SHARD_SIZE).map((c) => ({
      url: `${origin}/c/${c.slug}`,
      lastModified: c.updated_at ?? undefined,
    }));
  }

  // The "core" shard: static routes + every non-company entity (bounded sets —
  // a few thousand URLs at their largest, far under the 50k cap).
  const staticEntries: MetadataRoute.Sitemap = [
    { url: `${origin}/` },
    { url: `${origin}/companies` },
    { url: `${origin}/investors` },
    { url: `${origin}/new` },
    { url: `${origin}/themes` },
    { url: `${origin}/industry` },
    { url: `${origin}/trends` },
    // /trending is a permanent nav destination with a graceful empty state
    // (same posture as /new), so it is listed unconditionally even before
    // momentum_score lands on prod.
    { url: `${origin}/trending` },
    { url: `${origin}/about` },
    { url: `${origin}/stats` },
  ];

  const [
    investors,
    tags,
    states,
    alternatives,
    themes,
    industries,
    mapIndustries,
  ] = await Promise.all([
    listAllInvestorSlugs(),
    listAllTags(),
    listAllStates(),
    listAlternativesCompanySlugs(),
    listAllThemeSlugs(),
    listCanonicalIndustries(),
    listIndustriesWithMapCoords(),
  ]);

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

  // `themes` is already the de-thinned list — listAllThemeSlugs returns only
  // themes with ≥ MIN_THEME_MEMBER_COUNT members (mirroring the alternatives
  // threshold) — so thin one/two-company theme pages stay out of the sitemap.
  const themeEntries: MetadataRoute.Sitemap = themes.map((t) => ({
    url: `${origin}/themes/${t.slug}`,
    lastModified: t.updated_at ?? undefined,
  }));

  // `industries` is the canonical bucket list (≥ MIN_INDUSTRY_COMPANY_COUNT
  // companies each), the same gate the routes enforce, so no arbitrary
  // freeform-label page is listed. A rare industry with no funding AND no
  // sub-themes self-noindexes at the page level (its generateMetadata sets
  // robots.index=false), which Google honors over sitemap inclusion.
  const industryEntries: MetadataRoute.Sitemap = industries.map((i) => ({
    url: `${origin}/industry/${industryToSlug(i.group)}`,
  }));

  // Per-industry market-map URLs — self-gating: listIndustriesWithMapCoords
  // returns [] (thus no entries) until companies.map_x/map_y exist on prod, so
  // an empty map is never listed. Same "don't index empty maps" principle as
  // the industry block's noindex guard above.
  const mapEntries: MetadataRoute.Sitemap = mapIndustries.map((group) => ({
    url: `${origin}/map/${industryToSlug(group)}`,
  }));

  // Only surface the /map hub once there is at least one map to crawl to —
  // listing an empty hub pre-coords would be thin content.
  const mapHub: MetadataRoute.Sitemap =
    mapIndustries.length > 0 ? [{ url: `${origin}/map` }] : [];

  return [
    ...staticEntries,
    ...mapHub,
    ...investorEntries,
    ...tagEntries,
    ...locationEntries,
    ...alternativesEntries,
    ...themeEntries,
    ...industryEntries,
    ...mapEntries,
  ];
}
