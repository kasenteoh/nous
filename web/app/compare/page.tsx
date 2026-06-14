// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { getCompaniesForCompare } from "@/lib/queries";
import {
  formatDate,
  formatEmployeeRange,
  formatLocation,
  formatUsd,
} from "@/lib/format";
import { StatusBadge } from "@/components/StatusBadge";
import type { CompareCompany } from "@/lib/types";

export const metadata: Metadata = {
  title: "Compare companies",
  description:
    "Compare US software startups side by side — funding, headcount, investors, and competitors.",
  // The slug set is user-driven and infinite; keep crawlers on the canonical.
  alternates: { canonical: "/compare" },
};

// /compare?slugs=a,b,c — 2 to 4 companies.
const MIN_COMPARE = 2;
const MAX_COMPARE = 4;

type SearchParams = { slugs?: string | string[] };

function firstStr(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

/** Parse the comma-separated slug list, trimmed/deduped, capped at MAX_COMPARE. */
function parseSlugs(raw: string): string[] {
  const seen = new Set<string>();
  for (const part of raw.split(",")) {
    const s = part.trim();
    if (s) seen.add(s);
    if (seen.size >= MAX_COMPARE) break;
  }
  return [...seen];
}

/** A labeled row in the comparison table. */
function Row({
  label,
  companies,
  render,
}: {
  label: string;
  companies: CompareCompany[];
  render: (c: CompareCompany) => React.ReactNode;
}) {
  return (
    <tr className="border-b border-edge align-top">
      <th
        scope="row"
        className="py-3 pr-6 text-left text-sm font-medium text-ink-muted whitespace-nowrap"
      >
        {label}
      </th>
      {companies.map((c) => (
        <td key={c.slug} className="py-3 pr-6 text-sm text-ink-soft">
          {render(c)}
        </td>
      ))}
    </tr>
  );
}

export default async function ComparePage({
  searchParams,
}: {
  // Next.js 16: searchParams is a Promise and must be awaited.
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const slugs = parseSlugs(firstStr(sp.slugs));
  const companies =
    slugs.length >= 1 ? await getCompaniesForCompare(slugs) : [];

  const enough = companies.length >= MIN_COMPARE;

  return (
    <main className="flex-1 px-6 py-12 max-w-6xl mx-auto w-full">
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          Compare companies
        </h1>
        <p className="mt-3 text-ink-muted max-w-2xl">
          Side-by-side funding, headcount, investors, and competitors. Add 2–4
          companies via{" "}
          <code className="rounded bg-edge/40 px-1.5 py-0.5 text-sm">
            /compare?slugs=acme,globex
          </code>
          .
        </p>
      </header>

      {!enough ? (
        <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
          <p className="text-ink-muted">
            {slugs.length === 0
              ? "No companies selected to compare."
              : companies.length === 0
                ? "None of those companies are listed."
                : "Pick at least two listed companies to compare."}
          </p>
          <Link
            href="/companies"
            className="mt-4 inline-block text-sm text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent"
          >
            Browse companies →
          </Link>
        </div>
      ) : (
        <div className="overflow-x-auto -mx-6 px-6">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-edge">
                <th className="w-40" />
                {companies.map((c) => (
                  <th key={c.slug} scope="col" className="py-3 pr-6 text-left">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        href={`/c/${c.slug}`}
                        className="text-base font-semibold text-ink hover:underline underline-offset-2"
                      >
                        {c.name}
                      </Link>
                      <StatusBadge status={c.status} />
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <Row
                label="Industry"
                companies={companies}
                render={(c) => c.industryGroup ?? "—"}
              />
              <Row
                label="HQ"
                companies={companies}
                render={(c) =>
                  c.hqCity || c.hqState
                    ? formatLocation(c.hqCity, c.hqState)
                    : "—"
                }
              />
              <Row
                label="Founded"
                companies={companies}
                render={(c) => c.yearIncorporated ?? "—"}
              />
              <Row
                label="Employees"
                companies={companies}
                render={(c) =>
                  formatEmployeeRange(c.employeeCountMin, c.employeeCountMax)
                }
              />
              <Row
                label="Total raised"
                companies={companies}
                render={(c) =>
                  c.totalRaised != null ? (
                    <span className="font-mono text-money">
                      {formatUsd(c.totalRaised)}
                    </span>
                  ) : (
                    "—"
                  )
                }
              />
              <Row
                label="Latest round"
                companies={companies}
                render={(c) =>
                  c.latestRoundType || c.latestRoundAmount != null ? (
                    <span>
                      {c.latestRoundType ?? "Round"}
                      {c.latestRoundAmount != null && (
                        <>
                          {" · "}
                          <span className="font-mono text-money">
                            {formatUsd(c.latestRoundAmount)}
                          </span>
                        </>
                      )}
                      {c.latestRoundDate && (
                        <span className="block text-xs text-ink-muted">
                          {formatDate(c.latestRoundDate)}
                        </span>
                      )}
                    </span>
                  ) : (
                    "—"
                  )
                }
              />
              <Row
                label="Rounds on file"
                companies={companies}
                render={(c) => (c.roundCount > 0 ? c.roundCount : "—")}
              />
              <Row
                label="Investors"
                companies={companies}
                render={(c) =>
                  c.investors.length > 0 ? (
                    <ul className="space-y-0.5">
                      {c.investors.map((name) => (
                        <li key={name}>{name}</li>
                      ))}
                    </ul>
                  ) : (
                    "—"
                  )
                }
              />
              <Row
                label="Competitors"
                companies={companies}
                render={(c) =>
                  c.competitors.length > 0 ? (
                    <ul className="space-y-0.5">
                      {c.competitors.map((name) => (
                        <li key={name}>{name}</li>
                      ))}
                    </ul>
                  ) : (
                    "—"
                  )
                }
              />
              <Row
                label="Website"
                companies={companies}
                render={(c) =>
                  c.website ? (
                    <a
                      href={c.website}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-ink-soft hover:text-ink underline underline-offset-2 decoration-ink-faint break-all"
                    >
                      {c.website.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "")}
                    </a>
                  ) : (
                    "—"
                  )
                }
              />
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-10">
        <Link
          href="/companies"
          className="text-sm font-medium text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
        >
          ← Browse all companies
        </Link>
      </div>
    </main>
  );
}
