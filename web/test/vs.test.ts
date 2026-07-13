import { describe, expect, it } from "vitest";

import { canonicalVsPair, vsPath } from "@/lib/vs";

describe("canonicalVsPair", () => {
  it("orders two slugs lexicographically", () => {
    expect(canonicalVsPair("globex", "acme")).toEqual(["acme", "globex"]);
    expect(canonicalVsPair("acme", "globex")).toEqual(["acme", "globex"]);
  });

  it("is order-independent — both inputs map to the same pair", () => {
    expect(canonicalVsPair("b", "a")).toEqual(canonicalVsPair("a", "b"));
  });

  it("handles equal slugs without reordering", () => {
    expect(canonicalVsPair("acme", "acme")).toEqual(["acme", "acme"]);
  });
});

describe("vsPath", () => {
  it("builds the canonical /vs path regardless of input order", () => {
    expect(vsPath("globex", "acme")).toBe("/vs/acme/globex");
    expect(vsPath("acme", "globex")).toBe("/vs/acme/globex");
  });
});
