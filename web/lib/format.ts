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
 * Full US state / territory name → 2-letter USPS code. Keyed by the lowercased
 * full name so the lookup is case-insensitive (see {@link stateAbbrev}). Covers
 * all 50 states, DC, and the five inhabited territories the catalog might carry.
 * "District of Columbia" also appears under the "Washington DC" spellings the
 * LLM tends to emit.
 */
const US_STATE_ABBREV: Record<string, string> = {
  alabama: "AL",
  alaska: "AK",
  arizona: "AZ",
  arkansas: "AR",
  california: "CA",
  colorado: "CO",
  connecticut: "CT",
  delaware: "DE",
  "district of columbia": "DC",
  "washington dc": "DC",
  "washington d.c.": "DC",
  florida: "FL",
  georgia: "GA",
  hawaii: "HI",
  idaho: "ID",
  illinois: "IL",
  indiana: "IN",
  iowa: "IA",
  kansas: "KS",
  kentucky: "KY",
  louisiana: "LA",
  maine: "ME",
  maryland: "MD",
  massachusetts: "MA",
  michigan: "MI",
  minnesota: "MN",
  mississippi: "MS",
  missouri: "MO",
  montana: "MT",
  nebraska: "NE",
  nevada: "NV",
  "new hampshire": "NH",
  "new jersey": "NJ",
  "new mexico": "NM",
  "new york": "NY",
  "north carolina": "NC",
  "north dakota": "ND",
  ohio: "OH",
  oklahoma: "OK",
  oregon: "OR",
  pennsylvania: "PA",
  "rhode island": "RI",
  "south carolina": "SC",
  "south dakota": "SD",
  tennessee: "TN",
  texas: "TX",
  utah: "UT",
  vermont: "VT",
  virginia: "VA",
  washington: "WA",
  "west virginia": "WV",
  wisconsin: "WI",
  wyoming: "WY",
  // Inhabited US territories (USPS codes).
  "american samoa": "AS",
  guam: "GU",
  "northern mariana islands": "MP",
  "puerto rico": "PR",
  "u.s. virgin islands": "VI",
  "us virgin islands": "VI",
  "virgin islands": "VI",
};

/**
 * Normalize a US state value to its 2-letter USPS code for display.
 * Some `hq_state` values are stored as full names ("California") and others as
 * codes ("CA"); this collapses both to the code so locations render uniformly.
 *
 * - Full name (case-insensitive) → code:  "California" / "california" → "CA"
 * - Already a 2-letter code       → unchanged, uppercased:  "ca" → "CA"
 * - Unrecognized / empty          → returned unchanged (trimmed)
 *
 * Display-only: it never touches routing or stored data. Pure, no deps.
 */
export function stateAbbrev(value: string): string {
  const trimmed = value.trim();
  const mapped = US_STATE_ABBREV[trimmed.toLowerCase()];
  if (mapped) return mapped;
  // Already a 2-letter code (any case) — present it canonically uppercased so
  // "ca" and "Ca" don't slip through looking different from "CA". Longer
  // unknown strings (a city mistakenly in the state slot, a foreign region)
  // pass through untouched rather than being mangled.
  if (/^[A-Za-z]{2}$/.test(trimmed)) return trimmed.toUpperCase();
  return trimmed;
}

/**
 * Format a city + state pair for display.
 * Returns "—" when both are absent. State values are normalized to their USPS
 * code via {@link stateAbbrev} so "California" and "CA" render identically.
 * Examples: ("San Francisco", "CA")         → "San Francisco, CA"
 *           ("San Francisco", "California") → "San Francisco, CA"
 *           (null, "CA")                    → "CA"
 *           ("Austin", null)                → "Austin"
 */
export function formatLocation(
  city: string | null,
  state: string | null,
): string {
  const normalizedState = state ? stateAbbrev(state) : state;
  const parts = [city, normalizedState].filter(Boolean);
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

/**
 * Human label for a theme's trailing-2-quarter funding growth (Wave 3 E-3).
 * `growth` is the stored (recent − prior) / prior ratio; it is NULL when the
 * prior window had no funding, in which case the label derives from the two
 * sums instead of fabricating an infinite rate:
 * - growth 2.0        → "+200%"
 * - growth -0.75      → "−75%"
 * - null, recent > 0  → "new" (funding appeared from a zero base)
 * - null, recent == 0 → "—"  (no dated funding in either window)
 */
export function formatGrowthLabel(
  recentUsd: number,
  growth: number | null,
): string {
  if (growth == null) return recentUsd > 0 ? "new" : "—";
  const pct = Math.round(growth * 100);
  // U+2212 minus sign for negatives, matching the site's typographic dashes.
  return pct < 0 ? `−${Math.abs(pct)}%` : `+${pct}%`;
}
