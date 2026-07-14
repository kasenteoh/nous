// /industry — the SEO hub: every canonical industry_group bucket, each linking
// to its landing page. Ordered by recent funding (the biggest-money industries
// first — a stable, honest signal; the "hottest by growth" ranking is /trends'
// job, not this browse hub's), with a trailing-2-quarter growth chip for color.
// Server component; the bucket list is gated to industries with ≥3 catalog
// companies, so no thin single-company page is ever linked.

// Revalidate every 6 hours, matching the other index pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import {
  industryFundingMomentum,
  listCanonicalIndustries,
} from "@/lib/queries";
import { fundingGrowth } from "@/lib/funding";
import { formatGrowthLabel, formatUsd, growthToneClass } from "@/lib/format";
import { industryToSlug } from "@/lib/industry";

export const metadata: Metadata = {
  // The layout's title template appends " — nous".
  title: "Industries",
  description:
    "US software startup industries, each with funding momentum and the " +
    "companies building in it — derived from recorded funding rounds.",
  alternates: { canonical: "/industry" },
};

export default async function IndustriesPage() {
  const [industries, momentum] = await Promise.all([
    listCanonicalIndustries(),
    industryFundingMomentum(),
  ]);

  // Index the momentum rows by industry for the join below.
  const byGroup = new Map(momentum.map((m) => [m.industry_group, m]));

  // Sort by recent raised desc (industries with no recent funding fall to the
  // bottom, ordered by company count); ties break on name.
  const ranked = [...industries].sort((a, b) => {
    const ra = byGroup.get(a.group)?.recent_usd ?? 0;
    const rb = byGroup.get(b.group)?.recent_usd ?? 0;
    return rb - ra || b.count - a.count || a.group.localeCompare(b.group);
  });

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          Industries
        </h1>
        <p className="mt-2 text-sm text-ink-muted max-w-2xl leading-relaxed">
          Every industry we track, with the funding raised by its companies in
          the last two full quarters and how that compares with the two before
          — derived from recorded funding rounds, never hand-entered.
        </p>
      </header>

      {/* ── List ────────────────────────────────────────────────────────────── */}
      {ranked.length === 0 ? (
        <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
          <p className="text-ink-muted">No industries to show yet.</p>
        </div>
      ) : (
        <ul className="divide-y divide-edge border-y border-edge">
          {ranked.map((industry) => {
            const m = byGroup.get(industry.group);
            const recent = m?.recent_usd ?? 0;
            const growth = m ? fundingGrowth(recent, m.prior_usd) : null;
            const label = formatGrowthLabel(recent, growth);
            return (
              <li key={industry.group}>
                <Link
                  href={`/industry/${industryToSlug(industry.group)}`}
                  className="group block py-4 hover:bg-edge/30 transition-colors px-2 -mx-2"
                >
                  <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                    <span className="font-medium text-ink group-hover:underline underline-offset-2">
                      {industry.group}
                    </span>
                    {recent > 0 && (
                      <span
                        className={`shrink-0 font-mono text-sm ${growthToneClass(label)}`}
                        title={
                          growth != null
                            ? `${formatUsd(recent)} raised in the last 2 full quarters vs ${formatUsd(m?.prior_usd ?? 0)} in the 2 before`
                            : `${formatUsd(recent)} raised in the last 2 full quarters; none recorded in the 2 before`
                        }
                      >
                        {formatUsd(recent)} · {label}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-sm text-ink-muted">
                    {industry.count.toLocaleString("en-US")}{" "}
                    {industry.count === 1 ? "company" : "companies"}
                  </p>
                </Link>
              </li>
            );
          })}
        </ul>
      )}

      <p className="mt-8 text-xs text-ink-muted leading-relaxed max-w-2xl">
        Industry labels are assigned during enrichment; funding figures are
        derived from rounds recorded from public announcements and may be
        incomplete. Only industries with at least three companies are listed.
      </p>
    </main>
  );
}
