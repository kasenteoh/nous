// /themes/[slug] — one market theme: member companies (similarity-ordered
// CompanyCard grid), funding-by-quarter (server-rendered inline SVG, derived
// from the members' stored funding rounds), and the newest entrants. Server
// component throughout; excluded companies are already dropped in the query
// layer and never surface here.

// Revalidate every 6 hours, matching the company pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { CompanyCard } from "@/components/CompanyCard";
import { ThemeFundingChart } from "@/components/ThemeFundingChart";
import { formatDate, formatGrowthLabel, formatUsd } from "@/lib/format";
import { bucketFundingByQuarter } from "@/lib/funding";
import { getThemeBySlug } from "@/lib/queries";
import { newestEntrants } from "@/lib/themes";

type Props = {
  params: Promise<{ slug: string }>;
};

// How many quarters the funding chart spans (including the current one).
const CHART_QUARTERS = 8;

// ─── Metadata ─────────────────────────────────────────────────────────────────

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const data = await getThemeBySlug(slug);
  if (!data) {
    // The layout's title template appends " — nous".
    return { title: "Theme not found" };
  }
  const { theme } = data;
  const lead = theme.description ?? `${theme.name} companies`;
  return {
    title: theme.name,
    description: `${theme.company_count} US software startups in the ${theme.name} theme (${theme.industry_group}). ${lead}`,
    alternates: { canonical: `/themes/${slug}` },
  };
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function ThemePage({ params }: Props) {
  const { slug } = await params;
  const data = await getThemeBySlug(slug);
  if (!data) notFound();

  const { theme, members, rounds } = data;
  const growth = formatGrowthLabel(theme.funding_recent_usd, theme.funding_growth);
  const buckets = bucketFundingByQuarter(rounds, CHART_QUARTERS);
  const entrants = newestEntrants(members);

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="mb-10">
        <p className="mb-2 text-sm text-ink-muted">
          <Link
            href="/themes"
            className="hover:text-ink underline underline-offset-2 decoration-ink-faint"
          >
            ← All themes
          </Link>
        </p>
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          {theme.name}
        </h1>
        <p className="mt-2 text-sm text-ink-muted">
          {theme.industry_group} · {members.length}{" "}
          {members.length === 1 ? "company" : "companies"}
        </p>
        {theme.description && (
          <p className="mt-4 max-w-2xl text-base text-ink-soft leading-relaxed">
            {theme.description}
          </p>
        )}
        <p className="mt-4 font-mono text-sm text-ink-muted">
          Last 2 full quarters:{" "}
          <span className="text-money">{formatUsd(theme.funding_recent_usd)}</span>{" "}
          raised vs {formatUsd(theme.funding_prior_usd)} in the 2 before
          {growth !== "—" && <> ({growth})</>}
        </p>
      </header>

      {/* ── Funding by quarter (derived from stored rounds) ─────────────────── */}
      <section className="mb-12">
        <h2 className="text-lg font-semibold text-ink mb-4">
          Funding by quarter
        </h2>
        <ThemeFundingChart buckets={buckets} />
        <p className="mt-2 text-xs text-ink-muted">
          Derived from {rounds.length}{" "}
          {rounds.length === 1 ? "funding round" : "funding rounds"} recorded
          for member companies; undated rounds are not charted and coverage
          may be incomplete.
        </p>
      </section>

      {/* ── New entrants ────────────────────────────────────────────────────── */}
      {entrants.length > 0 && (
        <section className="mb-12">
          <h2 className="text-lg font-semibold text-ink mb-4">New entrants</h2>
          <ul className="divide-y divide-edge border-y border-edge">
            {entrants.map((m) => (
              <li key={m.slug}>
                <Link
                  href={`/c/${m.slug}`}
                  className="group flex items-center justify-between gap-4 py-3 hover:bg-edge/30 transition-colors px-2 -mx-2"
                >
                  <span className="font-medium text-ink group-hover:underline underline-offset-2">
                    {m.name}
                  </span>
                  <span className="shrink-0 font-mono text-xs text-ink-muted">
                    added {formatDate(m.created_at)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* ── Members (similarity-ordered) ────────────────────────────────────── */}
      <section className="mb-12">
        <h2 className="text-lg font-semibold text-ink mb-4">
          Companies in this theme
        </h2>
        {members.length === 0 ? (
          <p className="text-sm text-ink-muted">
            No member companies to show right now.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {members.map((m) => (
              <div key={m.slug}>
                <CompanyCard company={m} logoUrl={m.logo_url} />
                {/* Ranking disclosure, mirroring the similar-companies
                    module: how close this member sits to the theme center. */}
                <p className="mt-1 px-1 font-mono text-xs text-ink-muted">
                  {Math.round(m.similarity * 100)}% match to theme center
                </p>
              </div>
            ))}
          </div>
        )}
      </section>

      <p className="text-xs text-ink-muted leading-relaxed max-w-2xl">
        Membership is computed by clustering company descriptions; funding
        figures are derived from rounds recorded from public announcements.
      </p>
    </main>
  );
}
