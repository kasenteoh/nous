// Shared guard against LLM scratch-notes leaking out of a competitor's stored
// rationale (e.g. "Included temporarily for evaluation but should be
// dropped."). Both surfacing paths — the Competitors component on the company
// page and getAlternatives() behind /alternatives, its sitemap threshold, and
// its JSON-LD — must apply the SAME test, so the regex lives here exactly once
// (W-C.3; they used to be two hand-synced copies). This is a display-side
// guard; the durable fix is validating these out in the pipeline.
//
// Import-safe from both server and client modules: keep this file free of any
// import (especially "server-only").

export const COMPETITOR_META_LEAK =
  /should be dropped|for evaluation|temporar|placeholder|do not (include|display|show)|not a (real )?competitor/i;

/** True when a competitor row's text smells like leaked model reasoning. */
export function competitorLeaksMeta(row: {
  reasoning?: string | null;
  description?: string | null;
}): boolean {
  return (
    COMPETITOR_META_LEAK.test(row.reasoning ?? "") ||
    COMPETITOR_META_LEAK.test(row.description ?? "")
  );
}
