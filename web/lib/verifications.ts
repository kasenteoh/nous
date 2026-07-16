// Pure helpers for the "✓ Verified against source" affordance. A verification
// (pipeline `fact_verifications`) is keyed within a company by fact_kind +
// fact_ref: company-level facts (total_raised / status) use fact_ref = ''; a
// funding round uses the round's id. The web renders a ✓ ONLY for a `supported`
// verdict (already filtered in the query) whose source_url still matches the
// figure's CURRENT source AND whose verified claim still contains the figure
// being rendered — a stale verdict (re-sourced fact, or a corrected amount at
// the same source) never shows a badge.

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

/** What the page is actually rendering for the fact — the claim-drift guard
 *  compares this against the verified claim's text. */
export type ExpectedFact =
  | { kind: "amount"; amountUsd: number | string | null | undefined }
  | { kind: "status"; status: string };

// Mirror of the pipeline's status-claim phrases
// (pipeline/src/nous/pipeline/verify_sources.py _STATUS_CLAIM).
const STATUS_PHRASES: Record<string, string> = {
  acquired: "has been acquired",
  shut_down: "has shut down",
  ipo: "has gone public (IPO)",
};

/** Compact USD exactly as the pipeline's claim builder formats it
 *  (verify_sources._format_usd) — "$1.2B", "$110M", "$8.5M", "$500K". Parity is
 *  load-bearing for the containment check below; if the two ever disagree on a
 *  rounding tie the guard hides a ✓ it could have shown (the safe direction),
 *  never shows a wrong one. */
export function pipelineUsd(value: number): string {
  if (value >= 1e9) return `$${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) {
    const millions = value / 1e6;
    return millions >= 100
      ? `$${millions.toFixed(0)}M`
      : `$${millions.toFixed(1)}M`;
  }
  if (value >= 1e3) return `$${(value / 1e3).toFixed(0)}K`;
  return `$${value.toFixed(0)}`;
}

/** Does the verified claim still describe what the page renders? The pipeline
 *  claim always embeds the formatted amount (or the status phrase), so simple
 *  containment detects drift — e.g. a total corrected from $12M to $9M at the
 *  same source no longer matches, and the ✓ hides until the pipeline's
 *  stale-claim sweep re-verifies the new figure. */
export function claimMatchesExpected(
  claim: string,
  expected: ExpectedFact,
): boolean {
  if (expected.kind === "status") {
    return claim.includes(
      STATUS_PHRASES[expected.status] ?? `is ${expected.status}`,
    );
  }
  const amount =
    expected.amountUsd == null ? Number.NaN : Number(expected.amountUsd);
  // The pipeline never verifies a NULL/negative-amount fact at the current
  // version ("an undisclosed amount" claims are skipped), so nothing valid can
  // match — fail closed.
  if (!Number.isFinite(amount) || amount < 0) return false;
  return claim.includes(pipelineUsd(amount));
}

/** The verification for a fact IF it is still valid to show — i.e. present, its
 *  source_url matches the figure's current source, and its claim still contains
 *  the rendered figure. Returns null otherwise (no verification, a re-sourced
 *  fact, or a drifted claim — a ✓ would be stale). */
export function verifiedAgainst(
  lookup: Map<string, FactVerification>,
  factKind: string,
  factRef: string,
  currentSourceUrl: string | null | undefined,
  expected: ExpectedFact,
): FactVerification | null {
  if (!currentSourceUrl) return null;
  const v = lookup.get(verificationKey(factKind, factRef));
  if (!v || v.source_url !== currentSourceUrl) return null;
  if (!claimMatchesExpected(v.claim, expected)) return null;
  return v;
}
