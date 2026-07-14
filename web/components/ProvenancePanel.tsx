// Server component — the "Data & provenance" section on /c/[slug]. A pure
// TRUST-BUILDER, never a data-gap advertiser (spec
// docs/superpowers/specs/2026-07-14-provenance-ui-design.md): it always affirms
// sourcing + freshness and shows a POSITIVE-ONLY completeness badge that is
// hidden below threshold — never a negative "thin/husk" badge that advertises
// gaps. Read-only display; all data flows in via props. Returns null when none
// of its three parts would render (omit-when-empty, same convention as Sources /
// FounderBackground).
//
// Three parts:
//   1. Positive completeness badge (gated) — from company.completeness_score,
//      mirroring MomentumBadge (shared exported thresholds + a guard, returns
//      null when not gated in; the 📄 glyph carries the only color).
//   2. "Last verified N days ago" — MAX over the *present* per-stage freshness
//      stamps, computed read-time; omitted when none is present.
//   3. A plain-language sourcing line anchor-linking to the Sources section
//      (#sources), shown only when the company actually has ≥1 recorded source.

import type { CompanyRow } from "@/lib/types";
import { formatDate } from "@/lib/format";

// ─── Completeness badge (positive-only, gated) ────────────────────────────────

/**
 * Web-side completeness thresholds for the positive badge. The pipeline's
 * `completeness_score` (migration 0042, written by the compute-completeness
 * stage from `util.completeness` — the SOLE scorer) is in [0,1]: the share of a
 * company's key profile fields that are filled in. A single source of truth for
 * the badge label map, mirroring MOMENTUM_BADGE_THRESHOLD.
 *
 * Positive-only by design (locked decision): at/above RICH → "Richly
 * documented", at/above WELL → "Well documented", below WELL (or NULL/absent) →
 * NO badge. There is deliberately no negative badge — a low completeness score
 * shows nothing rather than advertising a data gap.
 */
export const COMPLETENESS_WELL_THRESHOLD = 0.5;
export const COMPLETENESS_RICH_THRESHOLD = 0.75;

/**
 * The positive completeness label for a score, or null when the score doesn't
 * clear the "well documented" bar (including NULL/undefined — companies scored
 * below threshold, or not yet scored / pre-migration, get no badge). Mirrors
 * MomentumBadge's `isHeatingUp` guard so callers render the badge without a
 * guard of their own.
 */
export function completenessLabel(
  score: number | null | undefined,
): "Richly documented" | "Well documented" | null {
  if (score == null) return null;
  if (score >= COMPLETENESS_RICH_THRESHOLD) return "Richly documented";
  if (score >= COMPLETENESS_WELL_THRESHOLD) return "Well documented";
  return null;
}

// ─── "Last verified" (read-time MAX of the per-stage freshness stamps) ─────────

/** The freshness stamps the "Last verified" line maxes over — `last_enriched_at`
 *  plus the per-stage `*_checked_at` / `*_resolved_at` timestamps. A structural
 *  subset of CompanyRow, so the full row is assignable. */
type VerificationTimestamps = Pick<
  CompanyRow,
  | "last_enriched_at"
  | "website_resolved_at"
  | "website_fallback_checked_at"
  | "news_checked_at"
  | "website_funding_checked_at"
  | "employee_count_checked_at"
>;

const MS_PER_DAY = 86_400_000;

/**
 * The most recent verification timestamp on the company and how many whole days
 * ago it was, or null when NONE of the stamps is present (→ the line is omitted;
 * we never fabricate a freshness we don't have). "Verified" = when the pipeline
 * last checked/re-derived a field, taken read-time as the MAX over the present
 * stamps. Unparseable values are skipped; `days` floors at 0 (a future stamp,
 * from clock skew, reads as "today" rather than negative).
 */
export function lastVerified(
  ts: VerificationTimestamps,
  now: Date = new Date(),
): { iso: string; days: number } | null {
  const candidates = [
    ts.last_enriched_at,
    ts.website_resolved_at,
    ts.website_fallback_checked_at,
    ts.news_checked_at,
    ts.website_funding_checked_at,
    ts.employee_count_checked_at,
  ];

  let maxMs = Number.NEGATIVE_INFINITY;
  let maxIso: string | null = null;
  for (const c of candidates) {
    if (!c) continue;
    const ms = Date.parse(c);
    if (Number.isNaN(ms)) continue;
    if (ms > maxMs) {
      maxMs = ms;
      maxIso = c;
    }
  }

  if (maxIso === null) return null;
  const days = Math.max(0, Math.floor((now.getTime() - maxMs) / MS_PER_DAY));
  return { iso: maxIso, days };
}

/** "Last verified today" / "… 1 day ago" / "… N days ago". */
function verifiedText(days: number): string {
  if (days <= 0) return "Last verified today";
  if (days === 1) return "Last verified 1 day ago";
  return `Last verified ${days} days ago`;
}

// ─── Panel ────────────────────────────────────────────────────────────────────

/** The fields the panel reads off the company. A structural subset of CompanyRow
 *  (so the page passes the full row), which also keeps it migration-order-free:
 *  absent columns read `undefined` and the corresponding part hides. */
type ProvenanceCompany = VerificationTimestamps &
  Pick<CompanyRow, "completeness_score">;

interface Props {
  company: ProvenanceCompany;
  /** True when the page collected ≥1 recorded source for this company; gates the
   *  sourcing line (and its #sources anchor is only meaningful when Sources
   *  renders). */
  hasSources: boolean;
}

export function ProvenancePanel({ company, hasSources }: Props) {
  const label = completenessLabel(company.completeness_score);
  const verified = lastVerified(company);

  // Omit-when-empty: nothing to affirm → render nothing at all.
  if (!label && !verified && !hasSources) return null;

  const badgeTitle =
    label === "Richly documented"
      ? "One of our most thoroughly documented company profiles"
      : "A well-documented company profile";

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Data &amp; provenance</h2>
      <div className="flex flex-col gap-2 text-sm text-ink-muted">
        {label && (
          <div>
            {/* Same muted pill vocabulary as MomentumBadge / StatusBadge; the 📄
                glyph carries the only color. */}
            <span
              className="rounded border border-edge px-2 py-0.5 text-xs text-ink-muted"
              title={badgeTitle}
            >
              📄 {label}
            </span>
          </div>
        )}

        {verified && (
          // title = the exact date the profile was last verified.
          <p title={formatDate(verified.iso)}>{verifiedText(verified.days)}</p>
        )}

        {hasSources && (
          <p>
            Every figure here links to a{" "}
            <a
              href="#sources"
              className="text-ink-soft underline underline-offset-2 decoration-ink-faint hover:text-ink"
            >
              recorded source
            </a>
            .
          </p>
        )}
      </div>
    </section>
  );
}
