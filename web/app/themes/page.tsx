// /themes — market themes ranked by trailing-2-quarter funding growth: the
// "what's heating up" page. Each theme is an embedding cluster the pipeline
// named (compute-themes); the growth figure is derived from the member
// companies' stored funding rounds, never a hand-entered number.

// Revalidate every 6 hours, matching the other index pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { listThemes } from "@/lib/queries";
import { formatGrowthLabel, formatUsd, growthToneClass } from "@/lib/format";

export const metadata: Metadata = {
  // The layout's title template appends " — nous".
  title: "Themes",
  description:
    "US software startup market themes ranked by funding growth, derived " +
    "from recorded funding rounds.",
  alternates: { canonical: "/themes" },
};

export default async function ThemesPage() {
  const themes = await listThemes();

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          Themes
        </h1>
        <p className="mt-2 text-sm text-ink-muted max-w-2xl leading-relaxed">
          Market themes across the catalog, grouped by what companies build
          and ranked by funding growth — the trailing two quarters of member
          funding against the two before, derived from recorded funding
          rounds.
        </p>
      </header>

      {/* ── List ────────────────────────────────────────────────────────────── */}
      {themes.length === 0 ? (
        <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
          <p className="text-ink-muted">
            No themes computed yet — they appear after the pipeline&apos;s
            monthly themes run.
          </p>
        </div>
      ) : (
        <ul className="divide-y divide-edge border-y border-edge">
          {themes.map((theme) => {
            const growth = formatGrowthLabel(
              theme.funding_recent_usd,
              theme.funding_growth,
            );
            return (
              <li key={theme.slug}>
                <Link
                  href={`/themes/${theme.slug}`}
                  className="group block py-4 hover:bg-edge/30 transition-colors px-2 -mx-2"
                >
                  <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                    <span className="font-medium text-ink group-hover:underline underline-offset-2">
                      {theme.name}
                    </span>
                    <span
                      className={`shrink-0 font-mono text-sm ${growthToneClass(growth)}`}
                      title={
                        theme.funding_growth != null
                          ? `${formatUsd(theme.funding_recent_usd)} raised in the last 2 full quarters vs ${formatUsd(theme.funding_prior_usd)} in the 2 before`
                          : theme.funding_recent_usd > 0
                            ? `${formatUsd(theme.funding_recent_usd)} raised in the last 2 full quarters; none recorded in the 2 before`
                            : "No dated funding recorded in the last 4 full quarters"
                      }
                    >
                      {growth}
                    </span>
                  </div>
                  <p className="mt-1 text-sm text-ink-muted">
                    {theme.industry_group} · {theme.company_count}{" "}
                    {theme.company_count === 1 ? "company" : "companies"}
                  </p>
                  {theme.description && (
                    <p className="mt-1 text-sm text-ink-soft leading-snug">
                      {theme.description}
                    </p>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
      )}

      <p className="mt-8 text-xs text-ink-muted leading-relaxed max-w-2xl">
        Themes are computed monthly by clustering company descriptions;
        growth figures are derived from funding rounds recorded from public
        announcements and may be incomplete.
      </p>
    </main>
  );
}
