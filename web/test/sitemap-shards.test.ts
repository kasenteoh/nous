// Tests for the sitemap shard math (lib/sitemap-shards): shard-count scaling
// with the company count, the stable id scheme, and the always-≥1-company-
// shard guarantee (robots.txt must never advertise a 404).

import { describe, expect, it, vi, afterEach } from "vitest";
import {
  COMPANY_SHARD_SIZE,
  CORE_SITEMAP_ID,
  companyShardId,
  companyShardIndex,
  sitemapIds,
} from "@/lib/sitemap-shards";
import { countCompanies } from "@/lib/queries";

vi.mock("@/lib/queries", () => ({ countCompanies: vi.fn() }));
const mockedCount = vi.mocked(countCompanies);

afterEach(() => {
  vi.restoreAllMocks();
});

describe("companyShardId / companyShardIndex", () => {
  it("round-trips shard ids", () => {
    expect(companyShardId(0)).toBe("companies-0");
    expect(companyShardIndex("companies-0")).toBe(0);
    expect(companyShardIndex("companies-12")).toBe(12);
  });

  it("returns null for non-company ids", () => {
    expect(companyShardIndex(CORE_SITEMAP_ID)).toBeNull();
    expect(companyShardIndex("companies-")).toBeNull();
    expect(companyShardIndex("companies-x")).toBeNull();
  });
});

describe("sitemapIds", () => {
  it("emits core + one company shard for a small catalog", async () => {
    mockedCount.mockResolvedValue(5_000);
    expect(await sitemapIds()).toEqual([CORE_SITEMAP_ID, "companies-0"]);
  });

  it("adds shards as the catalog crosses shard-size multiples", async () => {
    mockedCount.mockResolvedValue(COMPANY_SHARD_SIZE + 1);
    expect(await sitemapIds()).toEqual([
      CORE_SITEMAP_ID,
      "companies-0",
      "companies-1",
    ]);
  });

  it("keeps one (empty-but-valid) company shard when the DB is unreachable", async () => {
    mockedCount.mockResolvedValue(0); // countCompanies degrades to 0
    expect(await sitemapIds()).toEqual([CORE_SITEMAP_ID, "companies-0"]);
  });
});
