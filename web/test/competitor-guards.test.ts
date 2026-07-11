import { describe, expect, it } from "vitest";

import { competitorLeaksMeta } from "@/lib/competitor-guards";

// The regex itself is exercised through the Competitors component tests and
// the getAlternatives query tests; this pins the shared predicate both now
// delegate to (W-C.3 — it used to be two hand-synced copies).
describe("competitorLeaksMeta", () => {
  it("flags leaked model scratch-notes in reasoning or description", () => {
    expect(
      competitorLeaksMeta({
        reasoning: "Included temporarily for evaluation but should be dropped.",
        description: null,
      }),
    ).toBe(true);
    expect(
      competitorLeaksMeta({
        reasoning: null,
        description: "Placeholder — do not display.",
      }),
    ).toBe(true);
  });

  it("passes ordinary competitor prose", () => {
    expect(
      competitorLeaksMeta({
        reasoning: "Both sell workflow automation to mid-market ops teams.",
        description: "Acme builds no-code workflow tooling.",
      }),
    ).toBe(false);
    expect(competitorLeaksMeta({ reasoning: null, description: null })).toBe(
      false,
    );
  });
});
