// Shared sitemap shard math for app/sitemap.ts (the shard content) and
// app/robots.ts (the Sitemap: lines). Next.js `generateSitemaps` serves each
// shard at /sitemap/[id].xml but emits NO index file, so robots.txt lists
// every shard explicitly — crawlers accept multiple Sitemap: lines.
//
// Shape: one "core" shard (static routes + every non-company entity — a few
// thousand URLs at most) plus N "companies-<i>" shards. Companies are the only
// unbounded set (BACKLOG: companies+tags approach Google's 50k-URLs-per-file
// cap), so they shard; everything else stays together.

import { countCompanies } from "@/lib/queries";

/** Google's hard cap is 50,000 URLs per sitemap file; shard companies well
 *  below it so growth between ISR windows can't overflow a shard. */
export const COMPANY_SHARD_SIZE = 40_000;

export const CORE_SITEMAP_ID = "core";

export function companyShardId(index: number): string {
  return `companies-${index}`;
}

/** Zero-based shard index for a companies-<i> id, or null for any other id. */
export function companyShardIndex(id: string): number | null {
  const match = /^companies-(\d+)$/.exec(id);
  return match ? Number(match[1]) : null;
}

/** All shard ids: ["core", "companies-0", …]. Always at least one company
 *  shard, so the URL set is stable when the DB is unreachable (that shard is
 *  then empty-but-valid — never a 404 for a URL robots.txt advertised). */
export async function sitemapIds(): Promise<string[]> {
  const total = await countCompanies();
  const shardCount = Math.max(1, Math.ceil(total / COMPANY_SHARD_SIZE));
  return [
    CORE_SITEMAP_ID,
    ...Array.from({ length: shardCount }, (_, i) => companyShardId(i)),
  ];
}
