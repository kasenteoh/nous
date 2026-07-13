import { describe, expect, it } from "vitest";

import { industryToSlug, resolveIndustrySlug } from "@/lib/industry";

describe("industryToSlug", () => {
  it("kebab-cases a label: lowercase, non-alphanumerics collapse to one hyphen", () => {
    expect(industryToSlug("AI Infrastructure")).toBe("ai-infrastructure");
    expect(industryToSlug("Fintech / Payments")).toBe("fintech-payments");
    expect(industryToSlug("AI / ML Infrastructure")).toBe(
      "ai-ml-infrastructure",
    );
  });

  it("trims leading and trailing separators", () => {
    expect(industryToSlug("  Health & Bio  ")).toBe("health-bio");
    expect(industryToSlug("/Edge/")).toBe("edge");
  });

  it("preserves digits", () => {
    expect(industryToSlug("Web3 Infrastructure")).toBe("web3-infrastructure");
  });
});

describe("resolveIndustrySlug", () => {
  const canonical = ["AI Infrastructure", "Fintech / Payments", "Healthcare"];

  it("maps a slug back to its canonical label", () => {
    expect(resolveIndustrySlug("ai-infrastructure", canonical)).toBe(
      "AI Infrastructure",
    );
    expect(resolveIndustrySlug("fintech-payments", canonical)).toBe(
      "Fintech / Payments",
    );
  });

  it("returns null for a slug that matches no canonical bucket (the hard gate)", () => {
    expect(resolveIndustrySlug("crypto", canonical)).toBeNull();
    expect(resolveIndustrySlug("", canonical)).toBeNull();
  });

  it("resolves deterministically to the first match on a slug collision", () => {
    // Two labels that slugify identically; sorted-list order decides the winner.
    const collision = ["AI ML", "AI/ML"];
    expect(resolveIndustrySlug("ai-ml", collision)).toBe("AI ML");
  });
});
