// Render tests for the /themes UI: the server-rendered SVG funding chart.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ThemeFundingChart } from "@/components/ThemeFundingChart";
import type { QuarterBucket } from "@/lib/funding";

function bucket(label: string, start: string, totalUsd: number): QuarterBucket {
  return { label, start, totalUsd };
}

const BUCKETS: QuarterBucket[] = [
  bucket("Q1 2026", "2026-01-01", 10_000_000),
  bucket("Q2 2026", "2026-04-01", 0),
  bucket("Q3 2026", "2026-07-01", 2_000_000),
];

describe("ThemeFundingChart", () => {
  it("renders an accessible SVG with one labelled bar per quarter", () => {
    const { container } = render(<ThemeFundingChart buckets={BUCKETS} />);

    const svg = screen.getByRole("img");
    expect(svg.getAttribute("aria-label")).toContain("Q1 2026: $10M");
    expect(svg.getAttribute("aria-label")).toContain("Q3 2026: $2M");

    // One rect per bucket (zero quarters keep a zero-height rect so the
    // series stays positionally complete), plus visible text labels.
    expect(container.querySelectorAll("rect")).toHaveLength(3);
    expect(screen.getByText("Q1 2026")).toBeDefined();
    expect(screen.getByText("$10M")).toBeDefined();
    expect(screen.getByText("$2M")).toBeDefined();
    // The zero quarter renders an em-dash amount, not a fabricated bar.
    expect(screen.getByText("—")).toBeDefined();
  });

  it("scales bars relative to the max quarter", () => {
    const { container } = render(<ThemeFundingChart buckets={BUCKETS} />);
    const rects = [...container.querySelectorAll("rect")];
    const heights = rects.map((r) => Number(r.getAttribute("height")));
    expect(heights[0]).toBeGreaterThan(heights[2]); // $10M taller than $2M
    expect(heights[1]).toBe(0); // zero quarter: no bar
    expect(heights[2]).toBeGreaterThanOrEqual(2); // small ≠ invisible
  });

  it("renders a no-data note instead of an empty chart", () => {
    render(
      <ThemeFundingChart
        buckets={[bucket("Q1 2026", "2026-01-01", 0)]}
      />,
    );
    expect(screen.queryByRole("img")).toBeNull();
    expect(
      screen.getByText(/No dated funding recorded/),
    ).toBeDefined();
  });
});
