import { describe, expect, it } from "vitest";

import { computeTotalRaised, dedupedRoundsTotal } from "@/lib/funding";

describe("dedupedRoundsTotal", () => {
  it("sums distinct rounds", () => {
    expect(
      dedupedRoundsTotal([
        { round_type: "seed", amount_raised: 2_000_000 },
        { round_type: "series_a", amount_raised: 10_000_000 },
      ]),
    ).toBe(12_000_000);
  });

  it("collapses Helion-style duplicates: one round re-reported by many articles", () => {
    // Helion's $465M Series G was stored 5 times from 5 articles; the naive
    // sum showed $2.3B. The (round_type, amount) key must count it once.
    const dupes = Array.from({ length: 5 }, () => ({
      round_type: "series_g",
      amount_raised: 465_000_000,
    }));
    expect(dedupedRoundsTotal(dupes)).toBe(465_000_000);
  });

  it("collapses duplicates even when round_type is null on every copy", () => {
    const dupes = [
      { round_type: null, amount_raised: 465_000_000 },
      { round_type: null, amount_raised: 465_000_000 },
    ];
    expect(dedupedRoundsTotal(dupes)).toBe(465_000_000);
  });

  it("keeps two genuinely distinct rounds that share an amount but differ in type", () => {
    expect(
      dedupedRoundsTotal([
        { round_type: "seed", amount_raised: 10_000_000 },
        { round_type: "series_a", amount_raised: 10_000_000 },
      ]),
    ).toBe(20_000_000);
  });

  it("ignores null amounts and handles string numerics from PostgREST", () => {
    expect(
      dedupedRoundsTotal([
        { round_type: "seed", amount_raised: null },
        { round_type: "series_a", amount_raised: "5000000" },
      ]),
    ).toBe(5_000_000);
  });
});

describe("computeTotalRaised", () => {
  it("stated total wins when it covers rounds the news missed", () => {
    const r = computeTotalRaised(285_000_000, [
      { round_type: "series_c", amount_raised: 100_000_000 },
    ]);
    expect(r.total).toBe(285_000_000);
    expect(r.statedWins).toBe(true);
    expect(r.hasTotal).toBe(true);
  });

  it("computed sum wins over a stale stated figure", () => {
    const r = computeTotalRaised(50_000_000, [
      { round_type: "series_a", amount_raised: 30_000_000 },
      { round_type: "series_b", amount_raised: 60_000_000 },
    ]);
    expect(r.total).toBe(90_000_000);
    expect(r.statedWins).toBe(false);
  });

  it("duplicate rounds cannot beat an accurate stated total", () => {
    // The Helion failure mode end-to-end: 5 duplicate rows must not push the
    // computed sum past the stated cumulative figure.
    const dupes = Array.from({ length: 5 }, () => ({
      round_type: "series_g",
      amount_raised: 465_000_000,
    }));
    const r = computeTotalRaised(1_030_000_000, dupes);
    expect(r.total).toBe(1_030_000_000);
    expect(r.statedWins).toBe(true);
  });

  it("reports nothing to show when there is neither a stated total nor amounts", () => {
    const r = computeTotalRaised(null, [
      { round_type: "seed", amount_raised: null },
    ]);
    expect(r.hasTotal).toBe(false);
    expect(r.hasComputed).toBe(false);
    expect(r.total).toBe(0);
  });

  it("a zero-dollar stated total still counts as a total (never fabricate, never hide data)", () => {
    const r = computeTotalRaised(0, []);
    expect(r.hasTotal).toBe(true);
    expect(r.statedWins).toBe(true);
    expect(r.total).toBe(0);
  });
});
