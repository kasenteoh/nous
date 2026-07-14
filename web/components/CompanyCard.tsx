// Shared server component for a single company card in the browse grid.
// Used by /companies, /tag/[tag], /location/[state], and /watchlist.

import Link from "next/link";
import { formatLocation, formatMomentumWhy } from "@/lib/format";
import { StatusBadge } from "@/components/StatusBadge";
import { MomentumBadge } from "@/components/MomentumBadge";
import { WatchlistButton } from "@/components/WatchlistButton";
import { CompareToggle } from "@/components/CompareBar";
import type { CompanyListRow } from "@/lib/types";

/**
 * Derive a one-character monogram for the logo fallback: the first
 * alphanumeric character of the name, uppercased. Punctuation/emoji-led names
 * ("·Foo", "🚀Bar") skip to the first letter or digit; a name with none (pure
 * symbols, or empty) falls back to "?" so the square is never blank.
 */
function monogram(name: string): string {
  const match = name.match(/[a-z0-9]/i);
  return (match?.[0] ?? "?").toUpperCase();
}

interface CompanyLogoProps {
  /** companies.logo_url — an external favicon URL, or null/absent (the common
   *  case until the pipeline backfills it). */
  logoUrl?: string | null;
  /** Company name — used for the alt text and the monogram fallback. */
  name: string;
  /** Rendered square size in px (width === height). */
  size: number;
}

/**
 * A company's logo as a fixed-size rounded square, with a stable monogram
 * fallback so the layout never shifts between the two states. When `logoUrl`
 * is present we render a plain lazy `<img>` with explicit dimensions (these are
 * external favicon URLs, so no next/image domain config); otherwise the
 * company's first initial sits in a themed bordered square.
 *
 * Exported so the company detail header can reuse the exact same treatment at a
 * larger size.
 */
export function CompanyLogo({ logoUrl, name, size }: CompanyLogoProps) {
  const dimension = { width: size, height: size };

  if (logoUrl) {
    return (
      // Plain <img>, not next/image, by design: these are tiny external favicon
      // URLs from arbitrary company domains, so next/image's remote-domain
      // allowlist and optimizer would add config + a proxy hop for no real
      // benefit at this size. Explicit width/height + lazy loading cover the
      // perf concern the rule guards against.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={logoUrl}
        alt={`${name} logo`}
        width={size}
        height={size}
        loading="lazy"
        // object-contain keeps non-square favicons from stretching; the bg +
        // border give transparent/dark icons a consistent themed plate that
        // matches the monogram fallback's footprint exactly.
        className="shrink-0 rounded border border-edge bg-canvas object-contain"
        style={dimension}
      />
    );
  }

  return (
    <span
      aria-hidden="true"
      className="flex shrink-0 select-none items-center justify-center rounded border border-edge bg-canvas font-semibold text-ink-muted"
      // Scale the initial to ~45% of the box so it reads at both card and
      // header sizes without a separate class per call site.
      style={{ ...dimension, fontSize: Math.round(size * 0.45) }}
    >
      {monogram(name)}
    </span>
  );
}

interface CompanyCardProps {
  company: CompanyListRow;
  /**
   * Optional `companies.logo_url`. The browse/portfolio projections
   * (CompanyListRow) don't carry it today, so this is omitted at every current
   * call site and the monogram fallback renders — wired as an optional prop so
   * the logo lights up automatically if a future query selects the column.
   */
  logoUrl?: string | null;
  /**
   * Optional pipeline momentum score in [0,1] (see MomentumBadge). Supplied
   * only by /trending; omitted at every other call site (browse, watchlist,
   * tag, location, alternatives, investor pages) so the badge never lights
   * there. Above the badge threshold it renders a "🔥 Heating up" pill.
   */
  momentumScore?: number | null;
  /**
   * Optional pre-worded momentum breakdown (["+40% team", "5 news mentions"]),
   * again /trending-only. Rendered as a compact "why" line via
   * {@link formatMomentumWhy}; absent/empty → no line.
   */
  momentumWhy?: string[];
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
export function CompanyCard({
  company,
  logoUrl,
  momentumScore,
  momentumWhy,
}: CompanyCardProps) {
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
            the h2 so the group-hover underline applies to the name only.
            The logo (or monogram fallback) leads the row, left of the name. */}
        <div className="flex flex-wrap items-center gap-2 pr-8">
          <CompanyLogo logoUrl={logoUrl} name={company.name} size={26} />
          <h2 className="font-semibold text-ink group-hover:underline underline-offset-2 leading-snug">
            {company.name}
          </h2>
          <StatusBadge status={company.status} />
          {/* Momentum pill — renders null below the threshold and whenever
              momentumScore is omitted (every call site except /trending). */}
          <MomentumBadge score={momentumScore} />
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

        {/* Momentum "why" line — the pipeline's pre-worded breakdown, joined
            with the site's " · " separator (mirrors the homepage strip). Shown
            only on /trending, where momentumWhy is supplied. */}
        {momentumWhy && momentumWhy.length > 0 && (
          <p className="mt-3 font-mono text-xs text-ink-muted">
            {formatMomentumWhy(momentumWhy)}
          </p>
        )}
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
