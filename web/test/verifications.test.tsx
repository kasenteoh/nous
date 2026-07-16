// Tests for the "✓ Verified against source" affordance: the lookup helpers
// (lib/verifications) and the VerifiedBadge component (supported-only,
// source-matched, claim-matched, quote-in-tooltip).

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VerifiedBadge } from "@/components/VerifiedBadge";
import type { FactVerification } from "@/lib/types";
import {
  buildVerificationLookup,
  claimMatchesExpected,
  pipelineUsd,
  verificationKey,
  verifiedAgainst,
} from "@/lib/verifications";

const TR: FactVerification = {
  fact_kind: "total_raised",
  fact_ref: "",
  source_url: "https://techcrunch.com/acme",
  claim: "Acme has raised a total of $12.0M.",
  supporting_quote: "raised $12 million",
};
const ROUND: FactVerification = {
  fact_kind: "funding_round",
  fact_ref: "round-1",
  source_url: "https://reuters.com/acme-b",
  claim: "Acme raised $40.0M in its Series B round.",
  supporting_quote: null,
};

describe("verification lookup helpers", () => {
  it("keys a fact by kind + ref", () => {
    expect(verificationKey("total_raised", "")).toBe("total_raised:");
    expect(verificationKey("funding_round", "abc")).toBe("funding_round:abc");
  });

  it("indexes verifications by (kind, ref); first occurrence wins", () => {
    const dup: FactVerification = { ...TR, source_url: "https://other.com/x" };
    const map = buildVerificationLookup([TR, ROUND, dup]);
    expect(map.size).toBe(2);
    expect(map.get("total_raised:")?.source_url).toBe(TR.source_url); // first wins
  });

  it("verifiedAgainst returns the verification only when the source matches", () => {
    const map = buildVerificationLookup([TR, ROUND]);
    const expected = { kind: "amount", amountUsd: 12_000_000 } as const;
    // matching source + matching claim → the verification
    expect(
      verifiedAgainst(
        map,
        "total_raised",
        "",
        "https://techcrunch.com/acme",
        expected,
      ),
    ).toBe(TR);
    // stale: the fact now cites a different source → null (no stale ✓)
    expect(
      verifiedAgainst(
        map,
        "total_raised",
        "",
        "https://newswire.com/moved",
        expected,
      ),
    ).toBeNull();
    // no current source, or no verification → null
    expect(verifiedAgainst(map, "total_raised", "", null, expected)).toBeNull();
    expect(
      verifiedAgainst(map, "status", "", "https://techcrunch.com/acme", {
        kind: "status",
        status: "acquired",
      }),
    ).toBeNull();
  });

  it("verifiedAgainst hides the ✓ when the claim has drifted from the rendered figure", () => {
    const map = buildVerificationLookup([TR, ROUND]);
    // The stored claim says $12.0M but the page now renders $9.0M (a corrected
    // amount at the SAME source) → null until the pipeline re-verifies.
    expect(
      verifiedAgainst(map, "total_raised", "", "https://techcrunch.com/acme", {
        kind: "amount",
        amountUsd: 9_000_000,
      }),
    ).toBeNull();
    // Round path: rendered amount matches the verified claim → shown.
    expect(
      verifiedAgainst(
        map,
        "funding_round",
        "round-1",
        "https://reuters.com/acme-b",
        { kind: "amount", amountUsd: "40000000" }, // numeric may arrive as string
      ),
    ).toBe(ROUND);
    // Null/absent amount can never match (the pipeline skips those facts).
    expect(
      verifiedAgainst(
        map,
        "funding_round",
        "round-1",
        "https://reuters.com/acme-b",
        { kind: "amount", amountUsd: null },
      ),
    ).toBeNull();
    // statedWins=false invariant: when the summed-rounds total wins, the page
    // renders a figure larger than the verified stated total. The source URL
    // usually already differs (sum cites the company site), but even when the
    // URLs coincide the claim guard hides the ✓ — the amounts format apart.
    expect(
      verifiedAgainst(map, "total_raised", "", "https://techcrunch.com/acme", {
        kind: "amount",
        amountUsd: 15_000_000, // computed sum, exceeds the verified $12.0M
      }),
    ).toBeNull();
  });
});

describe("claim-drift guard primitives", () => {
  it("pipelineUsd mirrors the pipeline claim formatter", () => {
    // Mirrors pipeline _format_usd (test_verify_sources.test_format_usd_scales).
    expect(pipelineUsd(12_400_000_000)).toBe("$12.4B");
    expect(pipelineUsd(110_000_000)).toBe("$110M");
    expect(pipelineUsd(8_500_000)).toBe("$8.5M");
    expect(pipelineUsd(500_000)).toBe("$500K");
    expect(pipelineUsd(950)).toBe("$950");
    // Known rounding-tie divergence, pinned: Python's :.1f rounds half-even
    // ($12.2M) while toFixed rounds ties up ($12.3M). The mismatch makes the
    // guard HIDE a ✓ it could have shown — fail-closed, never a wrong badge.
    expect(pipelineUsd(12_250_000)).toBe("$12.3M");
  });

  it("matches status claims by lifecycle phrase", () => {
    expect(
      claimMatchesExpected("Acme has been acquired.", "status", {
        kind: "status",
        status: "acquired",
      }),
    ).toBe(true);
    expect(
      claimMatchesExpected("Acme has been acquired.", "status", {
        kind: "status",
        status: "shut_down",
      }),
    ).toBe(false);
    // unmapped statuses use the pipeline's "is {status}" fallback
    expect(
      claimMatchesExpected("Acme is dormant.", "status", {
        kind: "status",
        status: "dormant",
      }),
    ).toBe(true);
  });

  it("never matches an amount against the claim's OTHER dollar figure", () => {
    // A funding-round claim carries a second figure (the valuation). If the
    // round amount drifts onto the valuation's value, bare containment would
    // false-match — the grammatical anchor ("raised $X") must prevent that.
    const claim =
      "Acme raised $5.0M in its Seed round at a $10.0M post-money valuation.";
    expect(
      claimMatchesExpected(claim, "funding_round", {
        kind: "amount",
        amountUsd: 10_000_000, // drifted amount == old valuation
      }),
    ).toBe(false);
    expect(
      claimMatchesExpected(claim, "funding_round", {
        kind: "amount",
        amountUsd: 5_000_000, // the actual verified amount still matches
      }),
    ).toBe(true);
    // total_raised anchors on "a total of $X"
    expect(
      claimMatchesExpected(
        "Acme has raised a total of $12.0M.",
        "total_raised",
        { kind: "amount", amountUsd: 12_000_000 },
      ),
    ).toBe(true);
  });
});

describe("VerifiedBadge", () => {
  it("renders a ✓ with the quote in the tooltip when verified", () => {
    render(<VerifiedBadge verification={TR} label="Total raised" />);
    expect(screen.getByText("✓")).toBeInTheDocument();
    // accessible label (not a bare mark)
    expect(
      screen.getByText("Total raised verified against source"),
    ).toBeInTheDocument();
    // the supporting quote rides on the title tooltip
    const wrapper = screen.getByText("✓").closest("span[title]");
    expect(wrapper?.getAttribute("title")).toContain("raised $12 million");
  });

  it("renders a generic tooltip when there is no quote", () => {
    render(<VerifiedBadge verification={ROUND} label="Funding round" />);
    const wrapper = screen.getByText("✓").closest("span[title]");
    expect(wrapper?.getAttribute("title")).toBe(
      "Funding round — verified against the cited source",
    );
  });

  it("renders nothing when there is no verification", () => {
    const { container } = render(
      <VerifiedBadge verification={null} label="Status" />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
