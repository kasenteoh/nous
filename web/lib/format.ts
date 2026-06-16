// Formatting helpers — pure functions, no deps, safe to call in server components.

/**
 * Format a USD dollar amount for display.
 * - >= 1 000 000 000 → "$1.5B"
 * - >= 1 000 000     → "$1.5M"
 * - >= 1 000         → "$500K"
 * - other            → "$123"
 * - null/undefined   → "—"
 */
export function formatUsd(amount: number | null | undefined): string {
  if (amount == null) return "—";

  const abs = Math.abs(amount);

  if (abs >= 1_000_000_000) {
    return `$${(amount / 1_000_000_000).toLocaleString("en-US", { maximumFractionDigits: 1 })}B`;
  }
  if (abs >= 1_000_000) {
    return `$${(amount / 1_000_000).toLocaleString("en-US", { maximumFractionDigits: 1 })}M`;
  }
  if (abs >= 1_000) {
    return `$${(amount / 1_000).toLocaleString("en-US", { maximumFractionDigits: 1 })}K`;
  }
  return `$${amount.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

/**
 * Format a USD dollar amount as its exact, fully-written figure — for tooltips
 * that disambiguate the rounded `formatUsd` short form (e.g. $1.51M and $1.49M
 * both render "$1.5M", and a "$12.4B" tile hides the real number).
 * - 12_400_000_000  → "$12,400,000,000"
 * - null/undefined  → "—"
 * Pure: thousands separators via `toLocaleString`, no fractional cents.
 */
export function formatUsdExact(amount: number | null | undefined): string {
  if (amount == null) return "—";
  return `$${amount.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

/**
 * Format an ISO date string (YYYY-MM-DD or full ISO timestamp) for display.
 * Returns "—" for null/undefined.
 * Example: "2026-05-12" → "May 12, 2026"
 */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";

  // Parse as UTC to avoid timezone-offset day shifts when given a date-only string.
  const date = new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
  if (isNaN(date.getTime())) return "—";

  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  }).format(date);
}

/**
 * Format an employee-count range for display.
 * - min & max → "11–50" (or "42" when equal)
 * - min only  → "11+"
 * - max only  → "≤50"
 * - neither   → "—"
 */
export function formatEmployeeRange(
  min: number | null | undefined,
  max: number | null | undefined,
): string {
  if (min != null && max != null) {
    return min === max ? `${min}` : `${min}–${max}`;
  }
  if (min != null) return `${min}+`;
  if (max != null) return `≤${max}`;
  return "—";
}

/**
 * Format a city + state pair for display.
 * Returns "—" when both are absent.
 * Examples: ("San Francisco", "CA") → "San Francisco, CA"
 *           (null, "CA")            → "CA"
 *           ("Austin", null)        → "Austin"
 */
export function formatLocation(
  city: string | null,
  state: string | null,
): string {
  const parts = [city, state].filter(Boolean);
  return parts.length > 0 ? parts.join(", ") : "—";
}

const DISCOVERED_VIA_LABELS: Record<string, string> = {
  vc_portfolio: "VC portfolio",
  techcrunch: "TechCrunch",
  news: "News",
};

/**
 * Human-readable label for a company's `discovered_via` value (e.g.
 * "vc_portfolio" → "VC portfolio"). Unknown keys fall back to the raw value,
 * title-cased, so new pipeline values self-heal in the UI rather than leaking
 * a raw enum.
 */
export function discoveredViaLabel(value: string): string {
  return (
    DISCOVERED_VIA_LABELS[value] ??
    value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}
