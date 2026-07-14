// /trending — "Heating up": the highest-momentum shown companies, ranked by
// the pipeline-computed momentum_score, each with a compact "why" line. Reuses
// CompanyCard. On-demand ISR (6h, matching every index page). Empty-state until
// momentum_score lands on prod (the query 400s → [] pre-migration, see
// listHeatingUpCompanies).
//
// NOTE the naming: the homepage "Trending now" strip is a DIFFERENT signal (the
// funding-gated spotlight pool, getTrendingCompanies). This page is momentum /
// acceleration — hence "Heating up" everywhere in the copy and the
// momentum-specific query/badge symbols.

// Revalidate every 6 hours, matching the other index pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { listHeatingUpCompanies } from "@/lib/queries";
import { formatDate } from "@/lib/format";
import { CompanyCard } from "@/components/CompanyCard";

export const metadata: Metadata = {
  // The layout's title template appends " — nous".
  title: "Heating up",
  description:
    "US software startups with the fastest-accelerating momentum right now — " +
    "ranked by recent hiring, news, and funding activity.",
  alternates: { canonical: "/trending" },
};

const HEATING_UP_LIMIT = 30;

export default async function TrendingPage() {
  const companies = await listHeatingUpCompanies(HEATING_UP_LIMIT);
  // The whole list is scored in one pipeline pass, so the top row's timestamp
  // is representative "as of" for the page.
  const asOf = companies[0]?.momentumComputedAt ?? null;

  return (
    <main className="flex-1 px-6 py-12 max-w-6xl mx-auto w-full">
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          Heating up
        </h1>
        <p className="mt-2 text-sm text-ink-muted max-w-2xl leading-relaxed">
          The US software startups accelerating fastest right now — ranked by
          momentum from recent hiring, news, and funding activity. Updated a few
          times a day.
          {asOf && (
            <span className="text-ink-faint">
              {" "}
              Momentum as of {formatDate(asOf)}.
            </span>
          )}
        </p>
      </header>

      {companies.length === 0 ? (
        <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
          <p className="text-ink-muted">
            No momentum scores yet — check back once the signal has warmed up.
          </p>
          <p className="mt-4 text-sm">
            <Link
              href="/new"
              className="text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
            >
              See what&rsquo;s new this week &rarr;
            </Link>
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {companies.map((company) => (
            <CompanyCard
              key={company.slug}
              company={company}
              logoUrl={company.logo_url}
              momentumScore={company.momentumScore}
              momentumWhy={company.momentumWhy}
            />
          ))}
        </div>
      )}
    </main>
  );
}
