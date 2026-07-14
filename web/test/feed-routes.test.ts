// Route-handler tests for the per-entity RSS feeds. The query layer is mocked
// (it is exercised against the Supabase mock in feed-queries.test.ts); here we
// assert the routes' behavior: a valid, well-formed RSS document on success, a
// truthful 404 for an unknown entity, and an empty-but-valid feed when Supabase
// is intentionally absent. isSupabaseConfigured() is the real function, driven
// by stubbed env.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET as companyFeed } from "@/app/c/[slug]/feed.xml/route";
import { GET as industryFeed } from "@/app/industry/[group]/feed.xml/route";
import { GET as investorFeed } from "@/app/investor/[slug]/feed.xml/route";
import {
  getCompanyBySlug,
  getInvestorBySlug,
  listCanonicalIndustries,
  listRecentFundingsByIndustry,
  listRecentFundingsForCompanySlugs,
  listRecentNewsByIndustry,
  listRecentNewsForCompanySlugs,
} from "@/lib/queries";
import type { CompanyDetail, InvestorDetail } from "@/lib/types";

vi.mock("@/lib/queries", async (importOriginal) => {
  // Keep real consts (e.g. FEED_IN_SLUGS_CAP); override only the DB functions.
  const actual = await importOriginal<typeof import("@/lib/queries")>();
  return {
    ...actual,
    getCompanyBySlug: vi.fn(),
    getInvestorBySlug: vi.fn(),
    listCanonicalIndustries: vi.fn(),
    listRecentFundingsByIndustry: vi.fn(),
    listRecentNewsByIndustry: vi.fn(),
    listRecentFundingsForCompanySlugs: vi.fn(),
    listRecentNewsForCompanySlugs: vi.fn(),
  };
});

/** Assert an RSS 2.0 document parses without error and carries a channel. */
function assertWellFormedRss(xml: string): Document {
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  expect(doc.getElementsByTagName("parsererror")).toHaveLength(0);
  expect(doc.querySelector("rss")).not.toBeNull();
  expect(doc.querySelector("channel > title")).not.toBeNull();
  return doc;
}

function req(path: string): Request {
  return new Request(`http://localhost:3000${path}`);
}

beforeEach(() => {
  // Reset call history on the module mocks so per-test "was/wasn't called"
  // assertions don't see calls leaked from earlier tests.
  vi.clearAllMocks();
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
  // Present by default; the degradation tests override to empty.
  vi.stubEnv("SUPABASE_URL", "https://x.supabase.co");
  vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

// ─── /c/[slug]/feed.xml ────────────────────────────────────────────────────────

describe("company feed route", () => {
  it("returns a well-formed RSS feed of the company's funding + news", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue({
      company: { slug: "acme", name: "Acme" },
      fundingRounds: [
        {
          round_type: "Series A",
          amount_raised: 12_000_000,
          announced_date: "2026-05-01",
        },
        // Undated round is skipped (no <pubDate>/guid date).
        { round_type: "Seed", amount_raised: 1_000_000, announced_date: null },
      ],
      news: [
        {
          id: "n-1",
          url: "https://news.test/acme",
          title: "Acme launches",
          source: "TechCrunch",
          published_date: "2026-05-02",
        },
      ],
      // Fields the route ignores.
      people: [],
      competitors: [],
      investors: [],
    } as unknown as CompanyDetail);

    const res = await companyFeed(req("/c/acme/feed.xml"), {
      params: Promise.resolve({ slug: "acme" }),
    });

    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe(
      "application/rss+xml; charset=utf-8",
    );
    const xml = await res.text();
    assertWellFormedRss(xml);
    expect(xml).toContain("<title>Acme — funding &amp; news on nous</title>");
    expect(xml).toContain(
      "<atom:link href=\"http://localhost:3000/c/acme/feed.xml\"",
    );
    expect(xml).toContain("Acme raised $12M (Series A)");
    expect(xml).toContain("news:n-1");
    // The undated round did not produce an item.
    expect(xml).not.toContain("Seed");
  });

  it("404s for a configured-but-unknown company", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(null);
    const res = await companyFeed(req("/c/ghost/feed.xml"), {
      params: Promise.resolve({ slug: "ghost" }),
    });
    expect(res.status).toBe(404);
  });

  it("degrades to an empty-but-valid feed when Supabase is absent (no 404/500)", async () => {
    vi.stubEnv("SUPABASE_URL", "");
    vi.stubEnv("SUPABASE_SERVICE_ROLE_KEY", "");
    const res = await companyFeed(req("/c/acme/feed.xml"), {
      params: Promise.resolve({ slug: "acme" }),
    });
    expect(res.status).toBe(200);
    const xml = await res.text();
    assertWellFormedRss(xml);
    expect(xml).not.toContain("<item>");
    // The query layer was never consulted.
    expect(getCompanyBySlug).not.toHaveBeenCalled();
  });
});

// ─── /industry/[group]/feed.xml ────────────────────────────────────────────────

describe("industry feed route", () => {
  beforeEach(() => {
    vi.mocked(listCanonicalIndustries).mockResolvedValue([
      { group: "Fintech", count: 5 },
    ]);
  });

  it("returns a well-formed feed for a canonical industry slug", async () => {
    vi.mocked(listRecentFundingsByIndustry).mockResolvedValue([
      {
        companySlug: "acme",
        companyName: "Acme",
        round_type: "Series A",
        amount_raised: 12_000_000,
        announced_date: "2026-05-01",
      },
    ]);
    vi.mocked(listRecentNewsByIndustry).mockResolvedValue([]);

    const res = await industryFeed(req("/industry/fintech/feed.xml"), {
      params: Promise.resolve({ group: "fintech" }),
    });

    expect(res.status).toBe(200);
    const xml = await res.text();
    assertWellFormedRss(xml);
    expect(xml).toContain("<title>Fintech — funding &amp; news on nous</title>");
    expect(xml).toContain("Acme raised $12M (Series A)");
    // The canonical label resolved from the slug drives the resolver.
    expect(listRecentFundingsByIndustry).toHaveBeenCalledWith(
      "Fintech",
      expect.any(Number),
    );
  });

  it("404s for a non-canonical industry slug", async () => {
    const res = await industryFeed(req("/industry/bogus/feed.xml"), {
      params: Promise.resolve({ group: "bogus" }),
    });
    expect(res.status).toBe(404);
  });
});

// ─── /investor/[slug]/feed.xml ─────────────────────────────────────────────────

describe("investor feed route", () => {
  it("returns a well-formed feed of the portfolio's funding + news", async () => {
    vi.mocked(getInvestorBySlug).mockResolvedValue({
      slug: "sequoia",
      name: "Sequoia",
      portfolio: [{ slug: "acme" }, { slug: "globex" }],
    } as unknown as InvestorDetail);
    vi.mocked(listRecentFundingsForCompanySlugs).mockResolvedValue([
      {
        companySlug: "acme",
        companyName: "Acme",
        round_type: "Series A",
        amount_raised: 12_000_000,
        announced_date: "2026-05-01",
      },
    ]);
    vi.mocked(listRecentNewsForCompanySlugs).mockResolvedValue([]);

    const res = await investorFeed(req("/investor/sequoia/feed.xml"), {
      params: Promise.resolve({ slug: "sequoia" }),
    });

    expect(res.status).toBe(200);
    const xml = await res.text();
    assertWellFormedRss(xml);
    expect(xml).toContain(
      "<title>Sequoia portfolio — funding &amp; news on nous</title>",
    );
    expect(xml).toContain("Acme raised $12M (Series A)");
    // The portfolio slug set is threaded into the scoped queries.
    expect(listRecentFundingsForCompanySlugs).toHaveBeenCalledWith(
      ["acme", "globex"],
      expect.any(Number),
    );
  });

  it("404s for a configured-but-unknown investor", async () => {
    vi.mocked(getInvestorBySlug).mockResolvedValue(null);
    const res = await investorFeed(req("/investor/ghost/feed.xml"), {
      params: Promise.resolve({ slug: "ghost" }),
    });
    expect(res.status).toBe(404);
  });
});
