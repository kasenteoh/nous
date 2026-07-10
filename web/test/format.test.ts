import { describe, expect, it } from "vitest";
import {
  discoveredViaLabel,
  formatDate,
  formatEmployeeRange,
  formatLocation,
  formatUsd,
  formatUsdExact,
  stateAbbrev,
} from "@/lib/format";

describe("formatUsd", () => {
  it("renders billions with at most one decimal", () => {
    expect(formatUsd(1_000_000_000)).toBe("$1B");
    expect(formatUsd(1_500_000_000)).toBe("$1.5B");
    expect(formatUsd(12_400_000_000)).toBe("$12.4B");
  });

  it("renders millions with at most one decimal", () => {
    expect(formatUsd(1_000_000)).toBe("$1M");
    expect(formatUsd(2_500_000)).toBe("$2.5M");
  });

  it("rounds nearby amounts to the same short form ($1.51M and $1.49M both read $1.5M)", () => {
    expect(formatUsd(1_510_000)).toBe("$1.5M");
    expect(formatUsd(1_490_000)).toBe("$1.5M");
  });

  it("renders thousands with a K suffix", () => {
    expect(formatUsd(1_000)).toBe("$1K");
    expect(formatUsd(500_000)).toBe("$500K");
  });

  it("renders sub-thousand amounts as whole dollars", () => {
    expect(formatUsd(999)).toBe("$999");
    expect(formatUsd(123.6)).toBe("$124");
    expect(formatUsd(0)).toBe("$0");
  });

  it("renders an em dash for null and undefined", () => {
    expect(formatUsd(null)).toBe("—");
    expect(formatUsd(undefined)).toBe("—");
  });
});

describe("formatUsdExact", () => {
  it("writes the full figure with thousands separators (the tooltip that disambiguates the rounded form)", () => {
    expect(formatUsdExact(12_400_000_000)).toBe("$12,400,000,000");
    expect(formatUsdExact(1_510_000)).toBe("$1,510,000");
  });

  it("drops fractional cents", () => {
    expect(formatUsdExact(1_234_567.89)).toBe("$1,234,568");
  });

  it("renders zero as $0 and null/undefined as an em dash", () => {
    expect(formatUsdExact(0)).toBe("$0");
    expect(formatUsdExact(null)).toBe("—");
    expect(formatUsdExact(undefined)).toBe("—");
  });
});

describe("formatDate", () => {
  it("formats a date-only ISO string without timezone day-shift", () => {
    expect(formatDate("2026-05-12")).toBe("May 12, 2026");
    // A date-only string is parsed as UTC midnight — the rendered day must
    // never shift even in timezones far behind UTC.
    expect(formatDate("2025-12-31")).toBe("December 31, 2025");
  });

  it("formats a full ISO timestamp in UTC", () => {
    expect(formatDate("2026-05-12T18:30:00Z")).toBe("May 12, 2026");
    expect(formatDate("2025-12-31T23:59:59Z")).toBe("December 31, 2025");
  });

  it("renders an em dash for null, undefined, empty, and unparseable input", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate(undefined)).toBe("—");
    expect(formatDate("")).toBe("—");
    expect(formatDate("not-a-date")).toBe("—");
  });
});

describe("formatEmployeeRange", () => {
  it("renders min–max when both bounds are known", () => {
    expect(formatEmployeeRange(11, 50)).toBe("11–50");
  });

  it("collapses an equal min and max to a single number", () => {
    expect(formatEmployeeRange(42, 42)).toBe("42");
  });

  it("renders min+ when only the lower bound is known", () => {
    expect(formatEmployeeRange(11, null)).toBe("11+");
  });

  it("renders ≤max when only the upper bound is known", () => {
    expect(formatEmployeeRange(null, 50)).toBe("≤50");
  });

  it("renders an em dash when neither bound is known", () => {
    expect(formatEmployeeRange(null, null)).toBe("—");
    expect(formatEmployeeRange(undefined, undefined)).toBe("—");
  });
});

describe("stateAbbrev", () => {
  it("maps full state names to USPS codes case-insensitively", () => {
    expect(stateAbbrev("California")).toBe("CA");
    expect(stateAbbrev("new york")).toBe("NY");
  });

  it("trims whitespace before lookup", () => {
    expect(stateAbbrev("  Texas  ")).toBe("TX");
  });

  it("normalizes the DC spellings the LLM emits", () => {
    expect(stateAbbrev("Washington DC")).toBe("DC");
    expect(stateAbbrev("Washington D.C.")).toBe("DC");
    expect(stateAbbrev("District of Columbia")).toBe("DC");
  });

  it("uppercases an existing two-letter code", () => {
    expect(stateAbbrev("ca")).toBe("CA");
    expect(stateAbbrev("Ny")).toBe("NY");
  });

  it("passes unrecognized longer values through unchanged (trimmed)", () => {
    expect(stateAbbrev("Ontario")).toBe("Ontario");
    expect(stateAbbrev(" Bavaria ")).toBe("Bavaria");
  });
});

describe("formatLocation", () => {
  it("joins city and normalized state with a comma", () => {
    expect(formatLocation("San Francisco", "CA")).toBe("San Francisco, CA");
    expect(formatLocation("San Francisco", "California")).toBe(
      "San Francisco, CA",
    );
  });

  it("renders whichever half is present when the other is null", () => {
    expect(formatLocation("Austin", null)).toBe("Austin");
    expect(formatLocation(null, "ca")).toBe("CA");
  });

  it("renders an em dash when both are absent", () => {
    expect(formatLocation(null, null)).toBe("—");
  });
});

describe("discoveredViaLabel", () => {
  it("uses the curated label for known pipeline values", () => {
    expect(discoveredViaLabel("vc_portfolio")).toBe("VC portfolio");
    expect(discoveredViaLabel("techcrunch")).toBe("TechCrunch");
    expect(discoveredViaLabel("news")).toBe("News");
  });

  it("title-cases unknown values instead of leaking the raw enum", () => {
    expect(discoveredViaLabel("hacker_news")).toBe("Hacker News");
    expect(discoveredViaLabel("some_new_source")).toBe("Some New Source");
  });
});
