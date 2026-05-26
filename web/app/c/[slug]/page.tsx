// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getCompanyBySlug } from "@/lib/queries";
import { formatDate, formatLocation, formatUsd } from "@/lib/format";

// ─── Types ────────────────────────────────────────────────────────────────────

type Props = {
  params: Promise<{ slug: string }>;
};

// ─── Metadata ─────────────────────────────────────────────────────────────────

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const detail = await getCompanyBySlug(slug);

  if (!detail) {
    return { title: "Company not found — nous" };
  }

  const { company } = detail;

  const parts: string[] = [];
  if (company.industry_group) parts.push(company.industry_group);
  if (company.hq_city || company.hq_state) {
    parts.push(formatLocation(company.hq_city, company.hq_state));
  }

  const description =
    parts.length > 0
      ? `${company.name}, ${parts.join(", ")}`
      : `${company.name} — SEC Form D filings and company information.`;

  return {
    title: `${company.name} — nous`,
    description,
  };
}

// ─── SEC URL helpers ──────────────────────────────────────────────────────────

/**
 * Build the per-filing EDGAR index URL.
 * cik is stored as a string (may include leading zeros). SEC expects the numeric
 * integer in the URL path.
 */
function secFilingUrl(cik: string | null, accessionNumber: string): string {
  if (!cik) return `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=D`;
  const numericCik = parseInt(cik, 10);
  // Accession number in URL uses dashes stripped: "0001234567-24-000001" → "0001234567-24-000001"
  // but the directory path uses no dashes: "0001234567-24-000001" → "000123456724000001"
  const undashed = accessionNumber.replace(/-/g, "");
  return `https://www.sec.gov/Archives/edgar/data/${numericCik}/${undashed}/`;
}

/** EDGAR company search URL (all Form D filings for a company). */
function secCompanyUrl(cik: string | null): string {
  if (!cik) return "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=D";
  return `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${cik}&type=D`;
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function CompanyPage({ params }: Props) {
  const { slug } = await params;
  const detail = await getCompanyBySlug(slug);

  if (!detail) {
    notFound();
  }

  const { company, filings, relatedPersons } = detail;

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Company header ─────────────────────────────────────────────── */}
      <header className="mb-10">
        <h1 className="text-4xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          {company.name}
        </h1>

        <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-2 text-sm text-zinc-500 dark:text-zinc-400">
          {(company.hq_city || company.hq_state) && (
            <div>
              <dt className="sr-only">Location</dt>
              <dd>{formatLocation(company.hq_city, company.hq_state)}</dd>
            </div>
          )}
          {company.year_incorporated && (
            <div>
              <dt className="sr-only">Year incorporated</dt>
              <dd>Est. {company.year_incorporated}</dd>
            </div>
          )}
          {company.industry_group && (
            <div>
              <dt className="sr-only">Industry</dt>
              <dd>{company.industry_group}</dd>
            </div>
          )}
        </dl>
      </header>

      {/* ── About ──────────────────────────────────────────────────────── */}
      {/* description fields fill in M2 */}

      {/* ── Filings table ──────────────────────────────────────────────── */}
      <section className="mb-12">
        <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-4">
          SEC Form D Filings
        </h2>

        {filings.length === 0 ? (
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            No filings on record.
          </p>
        ) : (
          <div className="overflow-x-auto -mx-6 px-6">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-zinc-200 dark:border-zinc-700 text-left text-zinc-500 dark:text-zinc-400">
                  <th className="py-2 pr-6 font-medium">Date</th>
                  <th className="py-2 pr-6 font-medium">Accession #</th>
                  <th className="py-2 pr-6 font-medium text-right">
                    Offering amount
                  </th>
                  <th className="py-2 pr-6 font-medium text-right">
                    Amount sold
                  </th>
                  <th className="py-2 font-medium text-right">Investors</th>
                </tr>
              </thead>
              <tbody>
                {filings.map((filing) => (
                  <tr
                    key={filing.id}
                    className="border-b border-zinc-100 dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-900/50"
                  >
                    <td className="py-3 pr-6 text-zinc-700 dark:text-zinc-300">
                      {formatDate(filing.filing_date)}
                    </td>
                    <td className="py-3 pr-6 font-mono text-xs">
                      <a
                        href={secFilingUrl(
                          company.cik,
                          filing.accession_number,
                        )}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-zinc-700 dark:text-zinc-300 underline underline-offset-2 hover:text-zinc-900 dark:hover:text-zinc-100"
                      >
                        {filing.accession_number}
                      </a>
                    </td>
                    <td className="py-3 pr-6 text-right text-zinc-700 dark:text-zinc-300">
                      {formatUsd(filing.offering_amount_total)}
                    </td>
                    <td className="py-3 pr-6 text-right text-zinc-700 dark:text-zinc-300">
                      {formatUsd(filing.amount_sold)}
                    </td>
                    <td className="py-3 text-right text-zinc-700 dark:text-zinc-300">
                      {filing.investors_count ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ── Related persons ─────────────────────────────────────────────── */}
      {relatedPersons.length > 0 && (
        <section className="mb-12">
          <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-4">
            Related Persons
          </h2>
          <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
            {relatedPersons.map((person) => (
              <li
                key={person.id}
                className="py-3 flex flex-col sm:flex-row sm:items-baseline gap-1 sm:gap-4"
              >
                <span className="font-medium text-zinc-900 dark:text-zinc-100">
                  {person.name}
                </span>
                <span className="text-sm text-zinc-500 dark:text-zinc-400">
                  {person.relationship}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* ── Sources footer — spec §7.3, §11 ─────────────────────────────── */}
      {filings.length > 0 && (
        <footer className="mt-16 pt-6 border-t border-zinc-200 dark:border-zinc-700">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-400 dark:text-zinc-500 mb-3">
            Sources
          </h2>
          <ul className="space-y-1 text-xs text-zinc-500 dark:text-zinc-400">
            {filings.map((filing) => (
              <li key={filing.id}>
                <a
                  href={secFilingUrl(company.cik, filing.accession_number)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline underline-offset-2 hover:text-zinc-700 dark:hover:text-zinc-300"
                >
                  SEC EDGAR Form D — {filing.accession_number} (
                  {formatDate(filing.filing_date)})
                </a>
              </li>
            ))}
            {company.cik && (
              <li>
                <a
                  href={secCompanyUrl(company.cik)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline underline-offset-2 hover:text-zinc-700 dark:hover:text-zinc-300"
                >
                  All Form D filings for {company.name} on EDGAR
                </a>
              </li>
            )}
          </ul>
        </footer>
      )}
    </main>
  );
}
