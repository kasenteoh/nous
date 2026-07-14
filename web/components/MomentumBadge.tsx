// Server component — a subtle "heating up" pill for high-momentum companies.
// Renders null below the threshold so callers can drop it in unconditionally,
// exactly like StatusBadge. Theme-safe: same tokens as the StatusBadge /
// "Discovered via" pills, with the 🔥 carrying the only color.

/**
 * Web-side momentum threshold for the badge. The pipeline's `momentum_score` is
 * in [0,1] where 0.5 = flat/neutral and higher = accelerating; we light the
 * badge only for genuinely accelerating companies. CALIBRATE once the score
 * distribution is observed on prod — start conservative. A single source of
 * truth reused by CompanyCard and the company detail header.
 */
export const MOMENTUM_BADGE_THRESHOLD = 0.65;

/** True when a score exists and clears the badge threshold. Absent/NULL scores
 *  (companies without enough history to score) never light the badge. */
export function isHeatingUp(score: number | null | undefined): boolean {
  return score != null && score >= MOMENTUM_BADGE_THRESHOLD;
}

/**
 * A muted "🔥 Heating up" pill (same styling as StatusBadge / the "Discovered
 * via" badge). Returns null below the threshold — including for NULL/undefined
 * scores — so callers render it unconditionally without a guard of their own.
 */
export function MomentumBadge({ score }: { score: number | null | undefined }) {
  if (!isHeatingUp(score)) return null;
  return (
    <span
      className="rounded border border-edge px-2 py-0.5 text-xs text-ink-muted"
      title="Momentum is accelerating — recent hiring, news, and funding activity"
    >
      🔥 Heating up
    </span>
  );
}
