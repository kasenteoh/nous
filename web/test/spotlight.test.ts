import { describe, expect, it } from "vitest";
import {
  mulberry32,
  scoreCompanies,
  seededShuffle,
  utcDateSeed,
} from "@/lib/spotlight";

describe("utcDateSeed", () => {
  it("encodes the UTC date as YYYYMMDD", () => {
    expect(utcDateSeed(new Date("2026-07-10T12:00:00Z"))).toBe(20260710);
    expect(utcDateSeed(new Date("2026-01-01T00:00:00Z"))).toBe(20260101);
  });

  it("uses the UTC day, not the local one, at the midnight boundary", () => {
    // 23:59 UTC and 00:01 UTC the next day are different seeds regardless of
    // the machine's timezone.
    expect(utcDateSeed(new Date("2026-07-10T23:59:00Z"))).toBe(20260710);
    expect(utcDateSeed(new Date("2026-07-11T00:01:00Z"))).toBe(20260711);
  });
});

describe("mulberry32", () => {
  it("is deterministic: the same seed yields the same sequence", () => {
    const a = mulberry32(42);
    const b = mulberry32(42);
    const seqA = [a(), a(), a(), a()];
    const seqB = [b(), b(), b(), b()];
    expect(seqA).toEqual(seqB);
  });

  it("yields values in [0, 1)", () => {
    const rand = mulberry32(20260710);
    for (let i = 0; i < 100; i++) {
      const v = rand();
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThan(1);
    }
  });

  it("different seeds diverge", () => {
    const a = mulberry32(1);
    const b = mulberry32(2);
    const seqA = [a(), a(), a()];
    const seqB = [b(), b(), b()];
    expect(seqA).not.toEqual(seqB);
  });
});

describe("seededShuffle (the daily spotlight order)", () => {
  const pool = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];

  it("is deterministic for a given date seed: same date → same order", () => {
    const seed = utcDateSeed(new Date("2026-07-10T08:00:00Z"));
    expect(seededShuffle(pool, seed)).toEqual(seededShuffle(pool, seed));
  });

  it("produces a different order on a different date", () => {
    const today = seededShuffle(pool, utcDateSeed(new Date("2026-07-10T08:00:00Z")));
    const tomorrow = seededShuffle(pool, utcDateSeed(new Date("2026-07-11T08:00:00Z")));
    expect(today).not.toEqual(tomorrow);
  });

  it("returns a permutation of the input (nothing added, dropped, or duplicated)", () => {
    const out = seededShuffle(pool, 20260710);
    expect([...out].sort()).toEqual([...pool].sort());
  });

  it("does not mutate the input array", () => {
    const input = ["a", "b", "c"];
    seededShuffle(input, 20260710);
    expect(input).toEqual(["a", "b", "c"]);
  });

  it("handles empty and single-element candidate lists", () => {
    expect(seededShuffle([], 20260710)).toEqual([]);
    expect(seededShuffle(["only"], 20260710)).toEqual(["only"]);
  });
});

describe("scoreCompanies", () => {
  const now = new Date("2026-07-10T00:00:00Z");

  it("scores a round announced today at full recency weight plus the amount bonus", () => {
    const scores = scoreCompanies(
      [
        {
          company_id: "c1",
          round_type: "Series A",
          amount_raised: 1_000_000, // log10(1e6)/3 = 2
          announced_date: "2026-07-10",
        },
      ],
      new Map(),
      new Set(),
      now,
    );
    const entry = scores.get("c1");
    expect(entry).toBeDefined();
    expect(entry?.score).toBeCloseTo(3 + 2, 5);
  });

  it("gives no recency credit to a round older than the 120-day window", () => {
    const scores = scoreCompanies(
      [
        {
          company_id: "c1",
          round_type: "Seed",
          amount_raised: null, // no amount bonus either
          announced_date: "2025-07-10", // 365 days ago
        },
      ],
      new Map(),
      new Set(),
      now,
    );
    expect(scores.get("c1")?.score).toBe(0);
  });

  it("keeps the best-scoring round but tracks the most recent round's type", () => {
    const scores = scoreCompanies(
      [
        {
          company_id: "c1",
          round_type: "Series B",
          amount_raised: 50_000_000, // big old round: higher funding score
          announced_date: "2026-05-01",
        },
        {
          company_id: "c1",
          round_type: "Bridge",
          amount_raised: null, // recent but small
          announced_date: "2026-07-09",
        },
      ],
      new Map(),
      new Set(),
      now,
    );
    const entry = scores.get("c1");
    // The facts row shows the latest round type even when an older round scored higher.
    expect(entry?.latestRoundType).toBe("Bridge");
    expect(entry?.latestRoundDate).toBe("2026-07-09");
  });

  it("adds 0.5 per news article, capped at 10 articles", () => {
    const scores = scoreCompanies(
      [],
      new Map([
        ["c1", 3],
        ["c2", 25],
      ]),
      new Set(),
      now,
    );
    expect(scores.get("c1")?.score).toBeCloseTo(1.5, 5);
    expect(scores.get("c2")?.score).toBeCloseTo(5, 5); // capped at 10 × 0.5
  });

  it("adds the freshness bonus for recently created companies", () => {
    const scores = scoreCompanies([], new Map(), new Set(["c1"]), now);
    expect(scores.get("c1")?.score).toBeCloseTo(1.5, 5);
    expect(scores.get("c1")?.latestRoundDate).toBeNull();
  });

  it("scores news-only and fresh-only companies that have no rounds at all", () => {
    const scores = scoreCompanies(
      [],
      new Map([["news-co", 2]]),
      new Set(["fresh-co"]),
      now,
    );
    expect([...scores.keys()].sort()).toEqual(["fresh-co", "news-co"]);
  });

  it("returns an empty map for empty inputs", () => {
    expect(scoreCompanies([], new Map(), new Set(), now).size).toBe(0);
  });
});
