// /map/[industry] — the market map for one industry_group: a static
// server-rendered SVG where each dot is a company, positioned from precomputed
// PCA coords (companies.map_x/map_y) and sized by its latest raise. Reads only
// those floats and renders — NO ML on this route (it must never import
// lib/embed-query.ts, and /map is deliberately absent from EMBEDDER_ROUTES in
// next.config.ts; see the #157 worklog).
//
// On-demand ISR, NOT generateStaticParams — no route pre-generates these (that
// would couple `next build` to the DB); the slug is gated to canonical buckets
// at request time instead, exactly like /industry/[group].

// Revalidate every 6 hours, matching the company pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { IndustryMap } from "@/components/IndustryMap";
import { industryToSlug, resolveIndustrySlug } from "@/lib/industry";
import {
  listCanonicalIndustries,
  listIndustryMapNodes,
  type MapCompanyNode,
} from "@/lib/queries";

type Props = { params: Promise<{ industry: string }> };

/**
 * The industry label + its map nodes for one slug, or null for a non-canonical
 * slug (→ 404). Called by BOTH generateMetadata and the page — Next doesn't
 * dedupe these Supabase calls, matching the /industry/[group] precedent; each
 * render is ISR-cached for 6h, so the double fetch is cheap.
 */
async function loadMap(
  slug: string,
): Promise<{ group: string; nodes: MapCompanyNode[] } | null> {
  const industries = await listCanonicalIndustries();
  const group = resolveIndustrySlug(
    slug,
    industries.map((i) => i.group),
  ); // HARD GATE — only canonical buckets get a page.
  if (!group) return null;
  const nodes = await listIndustryMapNodes(group);
  return { group, nodes };
}

// ─── Metadata ─────────────────────────────────────────────────────────────────

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { industry } = await params;
  const data = await loadMap(industry);
  if (!data) {
    // The layout's title template appends " — nous".
    return { title: "Map not found" };
  }
  const empty = data.nodes.length === 0;
  return {
    title: `${data.group} market map`,
    description: `A visual map of ${data.nodes.length} ${data.group} startups, positioned by similarity and sized by funding — by nous.`,
    alternates: { canonical: `/map/${industryToSlug(data.group)}` },
    // Don't index an empty map (mirrors /industry/[group]'s thin-content guard):
    // until coords land, every map is empty, so this keeps them out of the index.
    ...(empty ? { robots: { index: false, follow: true } } : {}),
  };
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function MapPage({ params }: Props) {
  const { industry } = await params;
  const data = await loadMap(industry);
  if (!data) notFound(); // non-canonical slug → 404
  const { group, nodes } = data;

  return (
    <main className="flex-1 px-6 py-12 max-w-5xl mx-auto w-full">
      <header className="mb-8">
        <p className="mb-2 text-sm text-ink-muted">
          <Link
            href={`/industry/${industryToSlug(group)}`}
            className="hover:text-ink underline underline-offset-2 decoration-ink-faint"
          >
            ← {group}
          </Link>
        </p>
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          {group} market map
        </h1>
        <p className="mt-2 text-sm text-ink-muted max-w-2xl">
          Each dot is a company, positioned so similar companies sit near each
          other and sized by its latest raise. Click a dot to open the company.
        </p>
      </header>
      <IndustryMap group={group} nodes={nodes} />
    </main>
  );
}
