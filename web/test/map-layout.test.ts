import { describe, expect, it } from "vitest";

import {
  fundingRadius,
  layoutNodes,
  MAX_LABELS,
  PAD,
  R_MAX,
  R_MIN,
  scaleAxis,
  VIEW_H,
  VIEW_W,
  type RawNode,
} from "@/lib/map-layout";

// Minimal RawNode factory; positions/funding overridden per test.
function node(overrides: Partial<RawNode> = {}): RawNode {
  return {
    slug: overrides.slug ?? "co",
    name: overrides.name ?? "Co",
    map_x: overrides.map_x ?? 0,
    map_y: overrides.map_y ?? 0,
    latest_round_amount: overrides.latest_round_amount ?? null,
  };
}

// ─── scaleAxis ─────────────────────────────────────────────────────────────────

describe("scaleAxis", () => {
  it("maps the min and max of a range onto the endpoints", () => {
    expect(scaleAxis(0, 0, 10, PAD, VIEW_W - PAD)).toBe(PAD);
    expect(scaleAxis(10, 0, 10, PAD, VIEW_W - PAD)).toBe(VIEW_W - PAD);
  });

  it("centers a degenerate (zero-width) range at the midpoint — no divide-by-zero", () => {
    const mid = scaleAxis(5, 5, 5, PAD, VIEW_W - PAD);
    expect(mid).toBe((PAD + (VIEW_W - PAD)) / 2);
    expect(Number.isNaN(mid)).toBe(false);
  });
});

// ─── fundingRadius ─────────────────────────────────────────────────────────────

describe("fundingRadius", () => {
  it("returns R_MIN for null, zero, and negative amounts", () => {
    expect(fundingRadius(null, 1_000_000)).toBe(R_MIN);
    expect(fundingRadius(0, 1_000_000)).toBe(R_MIN);
    expect(fundingRadius(-5, 1_000_000)).toBe(R_MIN);
  });

  it("returns R_MIN when maxAmount is non-positive (whole set unfunded)", () => {
    expect(fundingRadius(1_000_000, 0)).toBe(R_MIN);
    expect(fundingRadius(null, 0)).toBe(R_MIN);
  });

  it("returns R_MAX for the max-funded node", () => {
    expect(fundingRadius(1_000_000, 1_000_000)).toBe(R_MAX);
  });

  it("clamps amounts above maxAmount to R_MAX", () => {
    expect(fundingRadius(5_000_000, 1_000_000)).toBe(R_MAX);
  });

  it("is monotonic increasing in amount and stays within [R_MIN, R_MAX]", () => {
    const max = 1_000_000;
    const small = fundingRadius(100_000, max);
    const mid = fundingRadius(500_000, max);
    const big = fundingRadius(900_000, max);
    expect(small).toBeLessThan(mid);
    expect(mid).toBeLessThan(big);
    expect(small).toBeGreaterThanOrEqual(R_MIN);
    expect(big).toBeLessThanOrEqual(R_MAX);
  });
});

// ─── layoutNodes: geometry ─────────────────────────────────────────────────────

describe("layoutNodes geometry", () => {
  it("returns [] for empty input", () => {
    expect(layoutNodes([])).toEqual([]);
  });

  it("maps the coordinate extremes onto the padded viewBox corners", () => {
    const placed = layoutNodes([
      node({ slug: "lo", map_x: -5, map_y: 100 }),
      node({ slug: "hi", map_x: 5, map_y: 200 }),
    ]);
    // (-5,100) is the min on both axes → top-left padded corner.
    expect(placed[0].cx).toBe(PAD);
    expect(placed[0].cy).toBe(PAD);
    // (5,200) is the max on both axes → bottom-right padded corner.
    expect(placed[1].cx).toBe(VIEW_W - PAD);
    expect(placed[1].cy).toBe(VIEW_H - PAD);
  });

  it("centers a single node (degenerate range on both axes) with no NaN", () => {
    const [only] = layoutNodes([node({ map_x: 42, map_y: 7 })]);
    expect(only.cx).toBe(VIEW_W / 2);
    expect(only.cy).toBe(VIEW_H / 2);
    expect(Number.isNaN(only.cx)).toBe(false);
    expect(Number.isNaN(only.cy)).toBe(false);
  });

  it("centers the axis whose values are all equal while spreading the other", () => {
    const placed = layoutNodes([
      node({ slug: "a", map_x: 3, map_y: 0 }),
      node({ slug: "b", map_x: 3, map_y: 10 }),
    ]);
    // All-equal x → both centered horizontally.
    expect(placed[0].cx).toBe(VIEW_W / 2);
    expect(placed[1].cx).toBe(VIEW_W / 2);
    // y still spans the padded height.
    expect(placed[0].cy).toBe(PAD);
    expect(placed[1].cy).toBe(VIEW_H - PAD);
  });
});

// ─── layoutNodes: labels ───────────────────────────────────────────────────────

describe("layoutNodes labels", () => {
  it("always labels the first (highest-funding) node", () => {
    const placed = layoutNodes([
      node({ slug: "top", name: "Top", map_x: 0, map_y: 0 }),
      node({ slug: "mid", name: "Mid", map_x: 5, map_y: 5 }),
    ]);
    expect(placed[0].labeled).toBe(true);
  });

  it("skips a label whose box overlaps an already-placed label (first wins)", () => {
    // A and B share the exact same coords (both at the x/y min), so their label
    // boxes are identical → only the first is labeled. C sits far away.
    const placed = layoutNodes([
      node({ slug: "a", name: "Alpha", map_x: 0, map_y: 0 }),
      node({ slug: "b", name: "Bravo", map_x: 0, map_y: 0 }),
      node({ slug: "c", name: "Charlie", map_x: 10, map_y: 10 }),
    ]);
    const byId = new Map(placed.map((p) => [p.slug, p]));
    expect(byId.get("a")?.labeled).toBe(true);
    expect(byId.get("b")?.labeled).toBe(false); // overlaps A → dropped
    expect(byId.get("c")?.labeled).toBe(true);
  });

  it("caps the number of labels at MAX_LABELS even when all could fit", () => {
    // 30 well-separated nodes along the diagonal: adjacent labels never overlap,
    // so the only thing bounding the count is the MAX_LABELS clutter guard.
    const many = Array.from({ length: 30 }, (_, i) =>
      node({ slug: `n${i}`, name: `n${i}`, map_x: i, map_y: i }),
    );
    const placed = layoutNodes(many);
    const labeled = placed.filter((p) => p.labeled).length;
    expect(labeled).toBe(MAX_LABELS);
    expect(labeled).toBeLessThanOrEqual(MAX_LABELS);
  });
});
