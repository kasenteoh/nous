// Pure helpers for the "✓ Verified against source" affordance. A verification
// (pipeline `fact_verifications`) is keyed within a company by fact_kind +
// fact_ref: company-level facts (total_raised / status) use fact_ref = ''; a
// funding round uses the round's id. The web renders a ✓ ONLY for a `supported`
// verdict (already filtered in the query) whose source_url still matches the
// figure's CURRENT source — a stale verdict never shows a badge.

import type { FactVerification } from "@/lib/types";

/** Composite lookup key for a fact within a company. */
export function verificationKey(factKind: string, factRef: string): string {
  return `${factKind}:${factRef}`;
}

/** Index the company's `supported` verifications by (fact_kind, fact_ref). First
 *  occurrence wins (there is at most one per fact via the DB unique key). */
export function buildVerificationLookup(
  verifications: readonly FactVerification[],
): Map<string, FactVerification> {
  const map = new Map<string, FactVerification>();
  for (const v of verifications) {
    const key = verificationKey(v.fact_kind, v.fact_ref);
    if (!map.has(key)) map.set(key, v);
  }
  return map;
}

/** The verification for a fact IF it is still valid to show — i.e. present and
 *  its source_url matches the figure's current source. Returns null otherwise
 *  (no verification, or the fact has been re-sourced since it was verified, so a
 *  ✓ would be stale). */
export function verifiedAgainst(
  lookup: Map<string, FactVerification>,
  factKind: string,
  factRef: string,
  currentSourceUrl: string | null | undefined,
): FactVerification | null {
  if (!currentSourceUrl) return null;
  const v = lookup.get(verificationKey(factKind, factRef));
  if (!v || v.source_url !== currentSourceUrl) return null;
  return v;
}
