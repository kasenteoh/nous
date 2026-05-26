// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import Link from "next/link";
import { listCompanies } from "@/lib/queries";
import { formatDate, formatLocation, formatUsd } from "@/lib/format";

export default async function Home() {
  const companies = await listCompanies({ limit: 50, offset: 0 });

  return (
    <main className="flex-1 px-6 py-12 max-w-6xl mx-auto w-full">
      {/* ── Hero ──────────────────────────────────────────────────────────── */}
      <header className="mb-12">
        <h1 className="text-5xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          nous
        </h1>
        <p className="mt-3 text-lg text-zinc-500 dark:text-zinc-400 max-w-xl">
          US software startups, indexed from SEC filings.
        </p>
      </header>

      {/* ── Company grid ──────────────────────────────────────────────────── */}
      {companies.length === 0 ? (
        <div className="rounded-lg border border-dashed border-zinc-300 dark:border-zinc-700 px-8 py-14 text-center">
          <p className="text-zinc-500 dark:text-zinc-400">
            No companies indexed yet. Run the pipeline to ingest filings:
          </p>
          <pre className="mt-4 inline-block rounded bg-zinc-100 dark:bg-zinc-800 px-4 py-2 text-sm text-zinc-700 dark:text-zinc-300 font-mono">
            <code>nous ingest-filings --since YYYY-MM-DD</code>
          </pre>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {companies.map((company) => (
            <Link
              key={company.slug}
              href={`/c/${company.slug}`}
              className="group block rounded-lg border border-zinc-200 dark:border-zinc-800 p-5 hover:border-zinc-400 dark:hover:border-zinc-600 transition-colors"
            >
              <h2 className="font-semibold text-zinc-900 dark:text-zinc-100 group-hover:underline underline-offset-2 leading-snug">
                {company.name}
              </h2>

              {company.description_short && (
                <p className="mt-2 text-sm text-zinc-500 dark:text-zinc-400 line-clamp-2 leading-snug">
                  {company.description_short}
                </p>
              )}

              <dl className="mt-3 space-y-1 text-sm text-zinc-500 dark:text-zinc-400">
                {(company.hq_city || company.hq_state) && (
                  <div className="flex justify-between gap-2">
                    <dt className="sr-only">Location</dt>
                    <dd>{formatLocation(company.hq_city, company.hq_state)}</dd>
                  </div>
                )}
                {company.industry_group && (
                  <div>
                    <dt className="sr-only">Industry</dt>
                    <dd className="truncate">{company.industry_group}</dd>
                  </div>
                )}
                <div className="flex justify-between gap-2 pt-2 border-t border-zinc-100 dark:border-zinc-800">
                  <div>
                    <dt className="sr-only">Latest filing</dt>
                    <dd>{formatDate(company.latest_filing_date)}</dd>
                  </div>
                  {company.latest_offering_amount != null && (
                    <div className="text-right">
                      <dt className="sr-only">Offering amount</dt>
                      <dd className="font-medium text-zinc-700 dark:text-zinc-300">
                        {formatUsd(company.latest_offering_amount)}
                      </dd>
                    </div>
                  )}
                </div>
              </dl>
            </Link>
          ))}
        </div>
      )}
    </main>
  );
}
