// Pure-helper tests for the /themes surfaces: quarter bucketing (the SVG
// chart's data), the growth label, and the new-entrants ordering.

import { describe, expect, it } from "vitest";
import { bucketFundingByQuarter } from "@/lib/funding";
import { formatGrowthLabel } from "@/lib/format";
import { newestEntrants } from "@/lib/themes";
import type { ThemeMember } from "@/lib/types";

// Mid-Q3 2026 — matches the pipeline-side tests' reference date.
const NOW = new Date("2026-07-11T12:00:00Z");

describe("bucketFundingByQuarter", () => {
  it("buckets rounds into calendar quarters, oldest first, including the current one", () => {
    const buckets = bucketFundingByQuarter(
      [
        { announced_date: "2026-03-01", amount_raised: 10_000_000 },
        { announced_date: "2026-02-15", amount_raised: 5_000_000 },
        { announced_date: "2026-07-05", amount_raised: 2_000_000 }, // current Q
        { announced_date: "2024-01-01", amount_raised: 99_000_000 }, // outside
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
    expect(buckets.map((b) => b.totalUsd)).toEqual([0, 15_000_000, 0, 2_000_000]);
  });

  it("keeps zero quarters so the time axis has no gaps", () => {
    const buckets = bucketFundingByQuarter([], 8, NOW);
    expect(buckets).toHaveLength(8);
    expect(buckets[0].label).toEqual("Q4 2024"); // crosses year boundaries
    expect(buckets.every((b) => b.totalUsd === 0)).toBe(true);
  });

  it("ignores undated and unamounted rounds (unplaceable, never guessed)", () => {
    const buckets = bucketFundingByQuarter(
      [
        { announced_date: null, amount_raised: 7_000_000 },
        { announced_date: "2026-05-01", amount_raised: null },
        { announced_date: "2026-05-01", amount_raised: "3000000" }, // string numeric
      ],
      4,
      NOW,
    );
    const q2 = buckets.find((b) => b.label === "Q2 2026");
    expect(q2?.totalUsd).toBe(3_000_000);
    expect(buckets.reduce((sum, b) => sum + b.totalUsd, 0)).toBe(3_000_000);
  });

  it("keys buckets by the quarter's first day (stable render key)", () => {
    const [first] = bucketFundingByQuarter([], 1, NOW);
    expect(first.start).toBe("2026-07-01");
  });
});

describe("formatGrowthLabel", () => {
  it("formats measured growth as a signed percentage", () => {
    expect(formatGrowthLabel(15_000_000, 2)).toBe("+200%");
    expect(formatGrowthLabel(2_000_000, -0.75)).toBe("−75%");
    expect(formatGrowthLabel(5_000_000, 0)).toBe("+0%");
  });

  it("derives labels from the sums when growth is null (zero prior base)", () => {
    expect(formatGrowthLabel(1_000_000, null)).toBe("new");
    expect(formatGrowthLabel(0, null)).toBe("—");
  });
});

describe("newestEntrants", () => {
  function member(slug: string, createdAt: string): ThemeMember {
    return {
      slug,
      name: `Co ${slug}`,
      hq_city: null,
      hq_state: null,
      industry_group: null,
      description_short: null,
      status: "active",
      logo_url: null,
      similarity: 0.9,
      created_at: createdAt,
    };
  }

  it("returns the newest members first, capped", () => {
    const members = [
      member("old", "2025-01-01T00:00:00Z"),
      member("newest", "2026-07-01T00:00:00Z"),
      member("mid", "2026-01-01T00:00:00Z"),
    ];
    expect(newestEntrants(members, 2).map((m) => m.slug)).toEqual([
      "newest",
      "mid",
    ]);
    // Input order is untouched (no in-place sort of the caller's array).
    expect(members[0].slug).toBe("old");
  });

  it("drops members without a created_at rather than sorting them arbitrarily", () => {
    const members = [member("dated", "2026-01-01T00:00:00Z"), member("undated", "")];
    expect(newestEntrants(members).map((m) => m.slug)).toEqual(["dated"]);
  });
});
