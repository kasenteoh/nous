// Shared server component for a single company card in the browse grid.
// Used by /companies, /tag/[tag], /location/[state], and /watchlist.

import Link from "next/link";
import { formatLocation } from "@/lib/format";
import { StatusBadge } from "@/components/StatusBadge";
import { WatchlistButton } from "@/components/WatchlistButton";
import { CompareToggle } from "@/components/CompareBar";
import type { CompanyListRow } from "@/lib/types";

interface CompanyCardProps {
  company: CompanyListRow;
}

/**
 * A card linking to /c/[slug] with name, description, and location/industry
 * meta, plus a watchlist star (Task C3) and a Compare checkbox. The card stays a
 * server component; the two interactive controls are the only client islands
 * (WatchlistButton, CompareToggle). Both are SIBLINGS of the link — not nested —
 * because the whole card body is an <a> and an interactive control inside an <a>
 * is invalid HTML: the star is absolutely positioned in the corner, the Compare
 * toggle sits in a footer row below the linked region.
 */
export function CompanyCard({ company }: CompanyCardProps) {
  return (
    <div className="group relative flex flex-col rounded-lg border border-edge p-5 hover:border-ink-muted transition-colors">
      {/* Watchlist toggle — kept out of the link flow (absolute sibling). The
          right padding on the header below leaves room so it never overlaps
          the company name. */}
      <div className="absolute right-3 top-3 z-10">
        <WatchlistButton slug={company.slug} name={company.name} />
      </div>

      <Link href={`/c/${company.slug}`} className="block">
        {/* Status marker (Acquired / Shut down / IPO) is kept outside
            the h2 so the group-hover underline applies to the name only. */}
        <div className="flex flex-wrap items-center gap-2 pr-8">
          <h2 className="font-semibold text-ink group-hover:underline underline-offset-2 leading-snug">
            {company.name}
          </h2>
          <StatusBadge status={company.status} />
        </div>

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

      {/* Compare toggle — a sibling footer row (interactive controls can't nest
          in the card-body <a>). mt-auto pins it to the bottom so cards of
          differing text length keep their toggles aligned in the grid. */}
      <div className="mt-auto pt-3">
        <CompareToggle slug={company.slug} name={company.name} />
      </div>
    </div>
  );
}
