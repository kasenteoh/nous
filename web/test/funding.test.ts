import { describe, expect, it } from "vitest";

import {
  computeTotalRaised,
  dedupedRoundsTotal,
  fundingGrowth,
  quarterBucketsFromTotals,
} from "@/lib/funding";

// Mid-Q3 2026 — matches the pipeline-side + themes tests' reference date.
const NOW = new Date("2026-07-11T12:00:00Z");

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

describe("quarterBucketsFromTotals", () => {
  it("windows pre-aggregated RPC rows, oldest first, including the current quarter", () => {
    const buckets = quarterBucketsFromTotals(
      [
        { quarter_start: "2025-10-01", total_usd: 10_000_000 },
        { quarter_start: "2026-04-01", total_usd: 5_000_000 }, // current-ish
        { quarter_start: "2023-01-01", total_usd: 99_000_000 }, // outside window
      ],
      4,
      NOW,
    );
    expect(buckets.map((b) => b.label)).toEqual([
      "Q4 2025",
      "Q1 2026",
      "Q2 2026",
      "Q3 2026",
    ]);
    expect(buckets.map((b) => b.totalUsd)).toEqual([
      10_000_000, 0, 5_000_000, 0,
    ]);
  });

  it("fills omitted quarters with 0 so the time axis has no gaps", () => {
    const buckets = quarterBucketsFromTotals([], 8, NOW);
    expect(buckets).toHaveLength(8);
    expect(buckets[0].label).toBe("Q4 2024");
    expect(buckets.every((b) => b.totalUsd === 0)).toBe(true);
  });

  it("coerces numeric-string totals and skips null totals", () => {
    const buckets = quarterBucketsFromTotals(
      [
        { quarter_start: "2026-04-01", total_usd: "3000000" },
        { quarter_start: "2026-04-01", total_usd: null },
      ],
      4,
      NOW,
    );
    const q2 = buckets.find((b) => b.label === "Q2 2026");
    expect(q2?.totalUsd).toBe(3_000_000);
  });

  it("keys buckets by the quarter's first day (stable render key)", () => {
    const [first] = quarterBucketsFromTotals([], 1, NOW);
    expect(first.start).toBe("2026-07-01");
  });
});

describe("fundingGrowth", () => {
  it("returns the fractional growth when there is a prior base", () => {
    expect(fundingGrowth(15_000_000, 5_000_000)).toBe(2); // +200%
    expect(fundingGrowth(2_000_000, 8_000_000)).toBe(-0.75); // −75%
  });

  it("returns null when the prior window has no funding to divide by", () => {
    expect(fundingGrowth(10_000_000, 0)).toBeNull();
    expect(fundingGrowth(0, 0)).toBeNull();
    expect(fundingGrowth(5_000_000, -1)).toBeNull(); // defensive: no negative base
  });
});
