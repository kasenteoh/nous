// Tests for the "✓ Verified against source" affordance: the lookup helpers
// (lib/verifications) and the VerifiedBadge component (supported-only,
// source-matched, quote-in-tooltip).

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VerifiedBadge } from "@/components/VerifiedBadge";
import type { FactVerification } from "@/lib/types";
import {
  buildVerificationLookup,
  verificationKey,
  verifiedAgainst,
} from "@/lib/verifications";

const TR: FactVerification = {
  fact_kind: "total_raised",
  fact_ref: "",
  source_url: "https://techcrunch.com/acme",
  supporting_quote: "raised $12 million",
};
const ROUND: FactVerification = {
  fact_kind: "funding_round",
  fact_ref: "round-1",
  source_url: "https://reuters.com/acme-b",
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
    // matching source → the verification
    expect(
      verifiedAgainst(map, "total_raised", "", "https://techcrunch.com/acme"),
    ).toBe(TR);
    // stale: the fact now cites a different source → null (no stale ✓)
    expect(
      verifiedAgainst(map, "total_raised", "", "https://newswire.com/moved"),
    ).toBeNull();
    // no current source, or no verification → null
    expect(verifiedAgainst(map, "total_raised", "", null)).toBeNull();
    expect(
      verifiedAgainst(map, "status", "", "https://techcrunch.com/acme"),
    ).toBeNull();
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
