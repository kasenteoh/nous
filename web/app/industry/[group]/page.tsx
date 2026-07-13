// /industry/[group] — one industry_group landing page: a funding-by-quarter
// chart (server-rendered SVG from the 0036 RPC, so it can't truncate on the
// largest industries), the sub-themes inside the industry, and a funding-ranked
// preview of its companies that links out to the full filterable list. The
// chart + sub-themes are the ONLY net-new content over /companies?industry=X;
// a page with neither is noindex'd (hard thin-content guard) rather than
// competing with the filtered list for the same query.
//
// On-demand ISR, NOT generateStaticParams — no route pre-generates these (that
// would couple `next build` to the DB); the slug is gated to canonical buckets
// at request time instead.

// Revalidate every 6 hours, matching the company pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { CompanyCard } from "@/components/CompanyCard";
import { ThemeFundingChart } from "@/components/ThemeFundingChart";
import {
  fundingGrowth,
  quarterBucketsFromTotals,
  type QuarterBucket,
} from "@/lib/funding";
import { formatGrowthLabel, formatUsd } from "@/lib/format";
import { industryToSlug, resolveIndustrySlug } from "@/lib/industry";
import {
  fundingByQuarter,
  industryFundingMomentum,
  listCanonicalIndustries,
  listCompanies,
  listThemesByIndustry,
  type IndustryMomentumRow,
} from "@/lib/queries";
import type { CompanyListRow, ThemeListRow } from "@/lib/types";

type Props = {
  params: Promise<{ group: string }>;
};

// How many quarters the funding chart spans (including the current one).
const CHART_QUARTERS = 8;
// How many companies the preview grid shows before linking to the full list.
const PREVIEW_COUNT = 12;

interface IndustryPageData {
  group: string;
  count: number;
  buckets: QuarterBucket[];
  hasFunding: boolean;
  momentum: IndustryMomentumRow | null;
  themes: ThemeListRow[];
  companies: CompanyListRow[];
}

/**
 * Everything the page and its metadata render for one slug, or null for a
 * non-canonical slug (→ 404). Called by BOTH generateMetadata and the page —
 * Next doesn't dedupe these Supabase calls, matching the /themes/[slug]
 * precedent; each render is ISR-cached for 6h, so the double fetch is cheap.
 */
async function loadIndustry(slug: string): Promise<IndustryPageData | null> {
  const industries = await listCanonicalIndustries();
  const group = resolveIndustrySlug(
    slug,
    industries.map((i) => i.group),
  );
  if (!group) return null;

  const [quarterTotals, momentumRows, themes, companiesResult] =
    await Promise.all([
      fundingByQuarter(CHART_QUARTERS, group),
      industryFundingMomentum(),
      listThemesByIndustry(group),
      listCompanies({
        industry_group: group,
        sort: "funding_desc",
        limit: PREVIEW_COUNT,
      }),
    ]);

  const buckets = quarterBucketsFromTotals(quarterTotals, CHART_QUARTERS);
  return {
    group,
    // The company count from the SAME listCompanies call the preview uses, so
    // the header, the "See all N" link, and its /companies?industry=X
    // destination all report one identical number at render time (rather than a
    // separately-fetched keyset count that could drift under ISR staleness).
    count: companiesResult.total,
    buckets,
    hasFunding: buckets.some((b) => b.totalUsd > 0),
    momentum: momentumRows.find((row) => row.industry_group === group) ?? null,
    themes,
    companies: companiesResult.rows,
  };
}

// ─── Metadata ─────────────────────────────────────────────────────────────────

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { group: slug } = await params;
  const data = await loadIndustry(slug);
  if (!data) {
    // The layout's title template appends " — nous".
    return { title: "Industry not found" };
  }
  // Hard thin-content guard: with neither a funding chart nor sub-themes, the
  // page carries nothing /companies?industry=X doesn't — keep it out of the
  // index rather than compete with the filtered list for the same query.
  const thin = !data.hasFunding && data.themes.length === 0;
  return {
    title: `${data.group} startups`,
    description: `${data.count.toLocaleString("en-US")} US software startups in ${data.group}, with funding momentum and sub-themes — tracked by nous from VC portfolios and funding news.`,
    alternates: { canonical: `/industry/${industryToSlug(data.group)}` },
    ...(thin ? { robots: { index: false, follow: true } } : {}),
  };
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function IndustryPage({ params }: Props) {
  const { group: slug } = await params;
  const data = await loadIndustry(slug);
  if (!data) notFound();

  const { group, count, buckets, momentum, themes, companies } = data;

  const recent = momentum?.recent_usd ?? 0;
  const growth = momentum ? fundingGrowth(recent, momentum.prior_usd) : null;
  const growthLabel = formatGrowthLabel(recent, growth);
  const allCompaniesHref = `/companies?industry=${encodeURIComponent(group)}`;

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="mb-10">
        <p className="mb-2 text-sm text-ink-muted">
          <Link
            href="/industry"
            className="hover:text-ink underline underline-offset-2 decoration-ink-faint"
          >
            ← All industries
          </Link>
        </p>
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          {group}
        </h1>
        <p className="mt-2 text-sm text-ink-muted">
          {count.toLocaleString("en-US")}{" "}
          {count === 1 ? "company" : "companies"}
        </p>
        {recent > 0 && (
          <p className="mt-4 font-mono text-sm text-ink-muted">
            Last 2 full quarters:{" "}
            <span className="text-money">{formatUsd(recent)}</span> raised vs{" "}
            {formatUsd(momentum?.prior_usd ?? 0)} in the 2 before
            {growthLabel !== "—" && <> ({growthLabel})</>}
          </p>
        )}
      </header>

      {/* ── Funding by quarter (from the 0036 RPC) ──────────────────────────── */}
      <section className="mb-12">
        <h2 className="text-lg font-semibold text-ink mb-4">
          Funding by quarter
        </h2>
        <ThemeFundingChart buckets={buckets} />
        <p className="mt-2 text-xs text-ink-faint">
          Total raised by companies in this industry per quarter, derived from
          funding rounds recorded from public announcements; undated rounds are
          not charted and coverage may be incomplete.
        </p>
      </section>

      {/* ── Sub-themes ──────────────────────────────────────────────────────── */}
      {themes.length > 0 && (
        <section className="mb-12">
          <h2 className="text-lg font-semibold text-ink mb-4">Sub-themes</h2>
          <ul className="divide-y divide-edge border-y border-edge">
            {themes.map((theme) => {
              const label = formatGrowthLabel(
                theme.funding_recent_usd,
                theme.funding_growth,
              );
              return (
                <li key={theme.slug}>
                  <Link
                    href={`/themes/${theme.slug}`}
                    className="group block py-3 hover:bg-edge/30 transition-colors px-2 -mx-2"
                  >
                    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                      <span className="font-medium text-ink group-hover:underline underline-offset-2">
                        {theme.name}
                      </span>
                      <span className="shrink-0 font-mono text-xs text-ink-muted">
                        {theme.company_count}{" "}
                        {theme.company_count === 1 ? "company" : "companies"}
                        {label !== "—" && <> · {label}</>}
                      </span>
                    </div>
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
        </section>
      )}

      {/* ── Companies (funding-ranked preview) ──────────────────────────────── */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold text-ink mb-4">
          Companies in {group}
        </h2>
        {companies.length === 0 ? (
          <p className="text-sm text-ink-muted">
            No companies to show right now.
          </p>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {companies.map((company) => (
                <CompanyCard
                  key={company.slug}
                  company={company}
                  logoUrl={company.logo_url}
                />
              ))}
            </div>
            {count > companies.length && (
              <p className="mt-6">
                <Link
                  href={allCompaniesHref}
                  className="text-sm font-medium text-accent hover:underline underline-offset-2"
                >
                  See all {count.toLocaleString("en-US")} companies in {group} →
                </Link>
              </p>
            )}
          </>
        )}
      </section>
    </main>
  );
}
