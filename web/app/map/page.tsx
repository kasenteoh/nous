// /map — the market-map hub: every industry that has precomputed company
// coordinates, each linking to its /map/[industry] page. Mirrors /industry's
// hub. Until the pipeline fills companies.map_x/map_y (and those columns reach
// prod), listIndustriesWithMapCoords returns [] and this renders its empty
// state — so the hub never links to an empty map. Server component.

// Revalidate every 6 hours, matching the other index pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { industryToSlug } from "@/lib/industry";
import { listIndustriesWithMapCoords } from "@/lib/queries";

export const metadata: Metadata = {
  // The layout's title template appends " — nous".
  title: "Market maps",
  description:
    "Visual market maps of US software startups by industry — companies " +
    "positioned by similarity and sized by funding.",
  alternates: { canonical: "/map" },
};

export default async function MapHubPage() {
  const industries = await listIndustriesWithMapCoords();

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          Market maps
        </h1>
        <p className="mt-2 text-sm text-ink-muted max-w-2xl leading-relaxed">
          Each map plots an industry&apos;s companies by similarity — derived
          from their descriptions — and sizes every dot by its latest raise.
        </p>
      </header>

      {/* ── List ────────────────────────────────────────────────────────────── */}
      {industries.length === 0 ? (
        <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
          <p className="text-ink-muted">
            No maps yet — positions are being computed.
          </p>
        </div>
      ) : (
        <ul className="divide-y divide-edge border-y border-edge">
          {industries.map((group) => (
            <li key={group}>
              <Link
                href={`/map/${industryToSlug(group)}`}
                className="group block py-4 hover:bg-edge/30 transition-colors px-2 -mx-2"
              >
                <span className="font-medium text-ink group-hover:underline underline-offset-2">
                  {group}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
