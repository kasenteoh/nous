// Server component — a subtle "✓ Verified against source" affordance next to an
// already-sourced figure on /c/[slug] (total raised, status, each funding round).
// It renders ONLY when the pipeline's discriminative source-verification
// (verify-sources) returned a `supported` verdict for THIS fact against its
// CURRENT source — the caller resolves that via `verifiedAgainst`, so a
// non-supported, absent, or stale verdict passes `null` here and nothing shows.
//
// Positive-only by design: one false ✓ destroys the sourcing moat, so the badge
// exists exclusively for grounded `supported` verdicts (uncertain/unsupported
// never reach here). The supporting quote rides on the hover tooltip.

import type { ReactElement } from "react";
import type { FactVerification } from "@/lib/types";

interface Props {
  /** The valid verification for this fact (from `verifiedAgainst`), or null/undefined
   *  to render nothing. Supported-only + source-matched by the caller. */
  verification: FactVerification | null | undefined;
  /** What the figure is, for the accessible name + tooltip (e.g. "Total raised"). */
  label: string;
}

export function VerifiedBadge({
  verification,
  label,
}: Props): ReactElement | null {
  if (!verification) return null;
  const quote = verification.supporting_quote?.trim();
  const title = quote
    ? `${label} — verified against source: “${quote}”`
    : `${label} — verified against the cited source`;

  return (
    // text-money (the positive green) so the ✓ reads as an affirmation, subtle
    // and superscripted like the SourceLink ↗ it sits beside. aria-hidden glyph +
    // an sr-only label so screen readers announce the meaning, not a bare mark.
    <span title={title} className="ml-0.5 text-money">
      <span className="sr-only">{`${label} verified against source`}</span>
      <span
        aria-hidden
        className="relative -top-[0.35em] text-[10px] font-semibold leading-none"
      >
        ✓
      </span>
    </span>
  );
}
