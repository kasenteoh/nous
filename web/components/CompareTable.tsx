// Shared side-by-side comparison table. Renders 2–N companies as columns with a
// fixed set of labeled rows (industry, HQ, funding, investors, competitors, …).
// Used by /compare (2–4 user-picked companies via ?slugs=) and /vs/[a]/[b] (a
// head-to-head pair). Server component — pure display of already-fetched
// CompareCompany data, no data access of its own.

import Link from "next/link";
import { StatusBadge } from "@/components/StatusBadge";
import {
  formatDate,
  formatEmployeeRange,
  formatLocation,
  formatUsd,
} from "@/lib/format";
import type { CompareCompany } from "@/lib/types";

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

export function CompareTable({ companies }: { companies: CompareCompany[] }) {
  return (
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
              c.hqCity || c.hqState ? formatLocation(c.hqCity, c.hqState) : "—"
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
                  {c.website
                    .replace(/^https?:\/\/(www\.)?/, "")
                    .replace(/\/$/, "")}
                </a>
              ) : (
                "—"
              )
            }
          />
        </tbody>
      </table>
    </div>
  );
}
