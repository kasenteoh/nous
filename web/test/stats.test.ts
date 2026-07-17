// Tests for the /stats pure helpers (lib/stats) + formatRelativeTime.

import { describe, expect, it } from "vitest";
import {
  latestActivityAt,
  latestRunsByStage,
  runStatusToneClass,
  stageLabel,
} from "@/lib/stats";
import { formatRelativeTime } from "@/lib/format";
import type { PipelineRunRow } from "@/lib/types";

function run(overrides: Partial<PipelineRunRow>): PipelineRunRow {
  return {
    stage: "enrich-companies",
    started_at: "2026-07-17T12:00:00Z",
    finished_at: "2026-07-17T12:05:00Z",
    status: "success",
    inputs_seen: 30,
    rows_written: 30,
    ...overrides,
  };
}

describe("latestRunsByStage", () => {
  it("keeps only the newest run per stage, preserving order", () => {
    const rows = [
      run({ stage: "ingest-news", finished_at: "2026-07-17T12:00:00Z" }),
      run({ stage: "enrich-companies", finished_at: "2026-07-17T11:00:00Z" }),
      run({ stage: "ingest-news", finished_at: "2026-07-17T09:00:00Z" }), // older dup
    ];
    const latest = latestRunsByStage(rows);
    expect(latest.map((r) => r.stage)).toEqual([
      "ingest-news",
      "enrich-companies",
    ]);
    expect(latest[0].finished_at).toBe("2026-07-17T12:00:00Z");
  });

  it("returns [] for no runs, and null latest activity", () => {
    expect(latestRunsByStage([])).toEqual([]);
    expect(latestActivityAt([])).toBeNull();
  });
});

describe("stageLabel / tone", () => {
  it("maps known stages and falls back to the raw id", () => {
    expect(stageLabel("verify-sources")).toBe("Source verification");
    expect(stageLabel("some-future-stage")).toBe("some-future-stage");
  });

  it("success reads money-green; empty/error warn", () => {
    expect(runStatusToneClass("success")).toBe("text-money");
    expect(runStatusToneClass("empty")).toBe("text-warn");
    expect(runStatusToneClass("error")).toBe("text-warn");
  });
});

describe("formatRelativeTime", () => {
  const now = new Date("2026-07-17T12:00:00Z");
  it("buckets coarsely and never fabricates on bad input", () => {
    expect(formatRelativeTime("2026-07-17T11:59:30Z", now)).toBe("just now");
    expect(formatRelativeTime("2026-07-17T11:15:00Z", now)).toBe(
      "45 minutes ago",
    );
    expect(formatRelativeTime("2026-07-17T04:00:00Z", now)).toBe("8 hours ago");
    expect(formatRelativeTime("2026-07-14T12:00:00Z", now)).toBe("3 days ago");
    expect(formatRelativeTime(null, now)).toBe("—");
    expect(formatRelativeTime("not-a-date", now)).toBe("—");
  });
});
