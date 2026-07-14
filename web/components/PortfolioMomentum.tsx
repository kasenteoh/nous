// Server component — the "Portfolio momentum" lens on /investor/[slug].
// Read-only display; data flows in via props (getInvestorPortfolioMomentum,
// which aggregates the pipeline momentum_score / #181 across the investor's
// portfolio). Omitted entirely when nothing in the portfolio is heating up, so
// the section only ever shows a real signal (spec §11: unknown = hidden).

import Link from "next/link";

import type { InvestorPortfolioMomentum } from "@/lib/types";

interface Props {
  momentum: InvestorPortfolioMomentum | null;
}

export function PortfolioMomentum({ momentum }: Props) {
  // Only surface the section when there's an actual "heating up" signal; a
  // scored-but-cold portfolio renders nothing rather than a content-less box.
  if (!momentum || momentum.heatingUpCount === 0) {
    return null;
  }

  const { scoredCount, heatingUpCount, topHeatingUp } = momentum;
  // The noun agrees with the denominator ("N of M scored portfolio companies"),
  // not the numerator. scoredCount is always ≥ 1 here (heatingUpCount ≥ 1).
  const companyWord = scoredCount === 1 ? "company" : "companies";

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-1">Portfolio momentum</h2>
      <p className="text-sm text-ink-muted mb-4">
        {heatingUpCount} of {scoredCount.toLocaleString("en-US")} scored
        portfolio {companyWord} heating up right now.
      </p>

      <ul className="divide-y divide-edge">
        {topHeatingUp.map((company) => (
          <li
            key={company.slug}
            className="py-3 flex flex-col sm:flex-row sm:items-baseline gap-1 sm:gap-4"
          >
            <Link
              href={`/c/${company.slug}`}
              className="font-medium text-ink hover:underline underline-offset-2"
            >
              {company.name}
            </Link>
            {company.momentumWhy.length > 0 && (
              <span className="text-sm text-ink-muted">
                {company.momentumWhy.join(" · ")}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
