// /trends — the macro funding dashboard: catalog-wide funding by quarter, the
// hottest industries by trailing-2-quarter growth, and the biggest recent
// rounds. Reuses the 0036 momentum RPCs + ThemeFundingChart; this is where the
// "hottest by growth" ranking lives (the /industry hub ranks by absolute recent
// funding — a browse order — so the two surfaces don't say the same thing).
// Server component throughout; excluded companies never surface (filtered in
// the query layer).

// Revalidate every 6 hours, matching the other index pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { ThemeFundingChart } from "@/components/ThemeFundingChart";
import {
  fundingByQuarter,
  industryFundingMomentum,
  listBiggestRecentRounds,
  listCanonicalIndustries,
} from "@/lib/queries";
import { fundingGrowth, quarterBucketsFromTotals } from "@/lib/funding";
import {
  formatDate,
  formatGrowthLabel,
  formatUsd,
  growthToneClass,
} from "@/lib/format";
import { industryToSlug } from "@/lib/industry";

export const metadata: Metadata = {
  // The layout's title template appends " — nous".
  title: "Funding trends",
  description:
    "US software startup funding trends — momentum by quarter, the fastest-" +
    "growing industries, and the biggest recent rounds, from recorded funding.",
  alternates: { canonical: "/trends" },
};

// The macro chart spans 3 years (12 quarters, current one included).
const CHART_QUARTERS = 12;
// How many hottest industries + biggest rounds to feature.
const HOT_INDUSTRIES = 8;
const BIG_ROUNDS = 12;

export default async function TrendsPage() {
  const [quarterTotals, momentum, canonical, bigRounds] = await Promise.all([
    fundingByQuarter(CHART_QUARTERS),
    industryFundingMomentum(),
    listCanonicalIndustries(),
    listBiggestRecentRounds(BIG_ROUNDS),
  ]);

  const buckets = quarterBucketsFromTotals(quarterTotals, CHART_QUARTERS);

  // Only industries that have a landing page (canonical, ≥3 companies) get a
  // link — so no "hottest industry" row ever points at a 404. Rank by growth
  // desc (the /trends story), then recent funding; industries with no
  // measurable prior-window base (growth null) sort below any measured rate.
  const canonicalGroups = new Set(canonical.map((c) => c.group));
  const hottest = momentum
    .filter((m) => m.recent_usd > 0 && canonicalGroups.has(m.industry_group))
    .map((m) => ({ ...m, growth: fundingGrowth(m.recent_usd, m.prior_usd) }))
    .sort((a, b) => {
      // Measured growth first, ordered desc; null-growth rows fall to the end,
      // ordered by recent funding among themselves.
      if (a.growth == null && b.growth == null) return b.recent_usd - a.recent_usd;
      if (a.growth == null) return 1;
      if (b.growth == null) return -1;
      return b.growth - a.growth || b.recent_usd - a.recent_usd;
    })
    .slice(0, HOT_INDUSTRIES);

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="mb-10">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          Funding trends
        </h1>
        <p className="mt-2 text-sm text-ink-muted max-w-2xl leading-relaxed">
          How US software startup funding is moving across the catalog — total
          raised by quarter, the fastest-growing industries, and the biggest
          recent rounds. Every figure is derived from funding rounds recorded
          from public announcements, never hand-entered.
        </p>
      </header>

      {/* ── Funding by quarter (whole catalog) ──────────────────────────────── */}
      <section className="mb-12">
        <h2 className="text-lg font-semibold text-ink mb-4">
          Funding by quarter
        </h2>
        <ThemeFundingChart buckets={buckets} />
        <p className="mt-2 text-xs text-ink-faint">
          Total raised across all tracked companies per quarter; the most recent
          bar is the in-progress quarter and will keep filling. Undated rounds
          are not charted and coverage may be incomplete.
        </p>
      </section>

      {/* ── Hottest industries ──────────────────────────────────────────────── */}
      {hottest.length > 0 && (
        <section className="mb-12">
          <h2 className="text-lg font-semibold text-ink mb-4">
            Hottest industries
          </h2>
          <p className="mb-4 text-sm text-ink-muted max-w-2xl">
            Ranked by how the last two full quarters of funding compare with the
            two before.
          </p>
          <ul className="divide-y divide-edge border-y border-edge">
            {hottest.map((m) => {
              const label = formatGrowthLabel(m.recent_usd, m.growth);
              return (
                <li key={m.industry_group}>
                  <Link
                    href={`/industry/${industryToSlug(m.industry_group)}`}
                    className="group flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-3 hover:bg-edge/30 transition-colors px-2 -mx-2"
                  >
                    <span className="font-medium text-ink group-hover:underline underline-offset-2">
                      {m.industry_group}
                    </span>
                    <span
                      className={`shrink-0 font-mono text-sm ${growthToneClass(label)}`}
                      title={
                        m.growth != null
                          ? `${formatUsd(m.recent_usd)} raised in the last 2 full quarters vs ${formatUsd(m.prior_usd)} in the 2 before`
                          : `${formatUsd(m.recent_usd)} raised in the last 2 full quarters; none recorded in the 2 before`
                      }
                    >
                      {formatUsd(m.recent_usd)} · {label}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {/* ── Biggest recent rounds ───────────────────────────────────────────── */}
      {bigRounds.length > 0 && (
        <section className="mb-8">
          <h2 className="text-lg font-semibold text-ink mb-4">
            Biggest recent rounds
          </h2>
          <p className="mb-4 text-sm text-ink-muted max-w-2xl">
            The largest funding rounds recorded in the last six months.
          </p>
          <ul className="divide-y divide-edge border-y border-edge">
            {bigRounds.map((r, i) => (
              <li key={`${r.companySlug}-${r.announced_date}-${i}`}>
                <Link
                  href={`/c/${r.companySlug}`}
                  className="group flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-3 hover:bg-edge/30 transition-colors px-2 -mx-2"
                >
                  <span className="font-medium text-ink group-hover:underline underline-offset-2">
                    {r.companyName}
                  </span>
                  <span className="shrink-0 font-mono text-xs text-ink-muted">
                    <span className="text-money text-sm">
                      {formatUsd(r.amount_raised)}
                    </span>
                    {r.round_type ? ` · ${r.round_type}` : ""} ·{" "}
                    {formatDate(r.announced_date)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="mt-8 text-xs text-ink-faint leading-relaxed max-w-2xl">
        Funding figures are derived from rounds recorded from public
        announcements and may be incomplete; growth compares only complete
        quarters, so the in-progress quarter never skews a rate.
      </p>
    </main>
  );
}
