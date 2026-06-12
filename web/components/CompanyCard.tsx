// Shared server component for a single company card in the browse grid.
// Used by /companies, /tag/[tag], and /location/[state].

import Link from "next/link";
import { formatLocation } from "@/lib/format";
import type { CompanyListRow } from "@/lib/types";

interface CompanyCardProps {
  company: CompanyListRow;
}

/**
 * A card linking to /c/[slug] with name, description, and location/industry
 * meta. Pure server component — no interactivity, no "use client".
 */
export function CompanyCard({ company }: CompanyCardProps) {
  return (
    <Link
      href={`/c/${company.slug}`}
      className="group block rounded-lg border border-edge p-5 hover:border-ink-muted transition-colors"
    >
      <h2 className="font-semibold text-ink group-hover:underline underline-offset-2 leading-snug">
        {company.name}
      </h2>

      {company.description_short && (
        <p className="mt-2 text-sm text-ink-muted line-clamp-2 leading-snug">
          {company.description_short}
        </p>
      )}

      <dl className="mt-3 space-y-1 text-sm text-ink-muted">
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
      </dl>
    </Link>
  );
}
