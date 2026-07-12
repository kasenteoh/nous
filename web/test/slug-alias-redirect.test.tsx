// W-E.4: miss-path permanent redirects for merged-away slugs.
//
// The /c/[slug] and /alternatives/[slug] pages consult slug_aliases ONLY when
// their primary lookup misses, then permanentRedirect() to the survivor's
// current slug. Next's redirect/notFound are control-flow errors — calling the
// async page function directly (husk-test pattern) lets us assert on the
// thrown digest: `NEXT_REDIRECT;replace;<url>;308;` for permanentRedirect
// (see node_modules/next/dist/client/components/redirect.js) and
// `NEXT_HTTP_ERROR_FALLBACK;404` for notFound.

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CompanyPage from "@/app/c/[slug]/page";
import AlternativesPage from "@/app/alternatives/[slug]/page";
import {
  getAliasTargetSlug,
  getAlsoBackedBy,
  getAlternatives,
  getCompanyBySlug,
  getInvestorNameToSlugMap,
  getRelatedCompanies,
  getSimilarCompanies,
} from "@/lib/queries";
import type { CompanyDetail, CompanyRow } from "@/lib/types";

vi.mock("@/lib/queries", () => ({
  getAliasTargetSlug: vi.fn(),
  getAlsoBackedBy: vi.fn(),
  getAlternatives: vi.fn(),
  getCompanyBySlug: vi.fn(),
  getInvestorNameToSlugMap: vi.fn(),
  getRelatedCompanies: vi.fn(),
  getSimilarCompanies: vi.fn(),
}));

// Minimal live company for the zero-extra-queries case (husk-test fixture).
function company(overrides: Partial<CompanyRow> = {}): CompanyRow {
  return {
    id: "c-1",
    name: "Acme Robotics",
    slug: "acme-robotics",
    normalized_name: "acme robotics",
    description_short: null,
    description_long: null,
    primary_category: null,
    tags: null,
    website: null,
    logo_url: null,
    hq_city: null,
    hq_state: null,
    hq_country: null,
    year_incorporated: null,
    industry_group: null,
    employee_count_min: null,
    employee_count_max: null,
    employee_count_source: null,
    last_enriched_at: null,
    discovered_via: "vc_portfolio",
    status: "active",
    status_source_url: null,
    consecutive_scrape_failures: 0,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function detail(): CompanyDetail {
  return {
    company: company(),
    people: [],
    fundingRounds: [],
    competitors: [],
    investors: [],
    news: [],
  };
}

/** Await the page and return what it threw (redirect/notFound are throws). */
async function caught(promise: Promise<unknown>): Promise<{ digest?: string }> {
  try {
    await promise;
  } catch (err) {
    return err as { digest?: string };
  }
  throw new Error("expected the page to throw a control-flow error");
}

beforeEach(() => {
  // Reset call history so the zero-extra-queries assertion below sees only
  // its own test's calls, then re-arm the always-needed defaults.
  vi.clearAllMocks();
  vi.mocked(getInvestorNameToSlugMap).mockResolvedValue({});
  vi.mocked(getRelatedCompanies).mockResolvedValue([]);
  vi.mocked(getSimilarCompanies).mockResolvedValue([]);
  vi.mocked(getAlsoBackedBy).mockResolvedValue([]);
});

describe("/c/[slug] miss-path alias redirect", () => {
  it("308-permanentRedirects an aliased (merged-away) slug to the survivor", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(null);
    vi.mocked(getAliasTargetSlug).mockResolvedValue("acme-robotics");

    const err = await caught(
      CompanyPage({ params: Promise.resolve({ slug: "acme-inc" }) }),
    );
    expect(err.digest).toBe("NEXT_REDIRECT;replace;/c/acme-robotics;308;");
    expect(getAliasTargetSlug).toHaveBeenCalledWith("acme-inc");
  });

  it("404s an unknown slug with no alias (unchanged miss behavior)", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(null);
    vi.mocked(getAliasTargetSlug).mockResolvedValue(null);

    const err = await caught(
      CompanyPage({ params: Promise.resolve({ slug: "never-existed" }) }),
    );
    expect(err.digest).toBe("NEXT_HTTP_ERROR_FALLBACK;404");
  });

  it("404s instead of looping when an alias degenerately targets itself", async () => {
    // Write-time guards make a self-alias impossible; the page-side
    // target !== slug check is defense-in-depth against a redirect loop.
    vi.mocked(getCompanyBySlug).mockResolvedValue(null);
    vi.mocked(getAliasTargetSlug).mockResolvedValue("acme-inc");

    const err = await caught(
      CompanyPage({ params: Promise.resolve({ slug: "acme-inc" }) }),
    );
    expect(err.digest).toBe("NEXT_HTTP_ERROR_FALLBACK;404");
  });

  it("never queries slug_aliases for a live slug (zero extra queries)", async () => {
    vi.mocked(getCompanyBySlug).mockResolvedValue(detail());

    render(
      await CompanyPage({
        params: Promise.resolve({ slug: "acme-robotics" }),
      }),
    );

    expect(screen.getByText("Acme Robotics", { selector: "h1" })).toBeInTheDocument();
    expect(getAliasTargetSlug).not.toHaveBeenCalled();
  });
});

describe("/alternatives/[slug] miss-path alias redirect", () => {
  it("308-permanentRedirects an aliased slug to the survivor's alternatives page", async () => {
    vi.mocked(getAlternatives).mockResolvedValue(null);
    vi.mocked(getAliasTargetSlug).mockResolvedValue("acme-robotics");

    const err = await caught(
      AlternativesPage({ params: Promise.resolve({ slug: "acme-inc" }) }),
    );
    expect(err.digest).toBe(
      "NEXT_REDIRECT;replace;/alternatives/acme-robotics;308;",
    );
  });

  it("404s an unknown slug with no alias", async () => {
    vi.mocked(getAlternatives).mockResolvedValue(null);
    vi.mocked(getAliasTargetSlug).mockResolvedValue(null);

    const err = await caught(
      AlternativesPage({ params: Promise.resolve({ slug: "never-existed" }) }),
    );
    expect(err.digest).toBe("NEXT_HTTP_ERROR_FALLBACK;404");
  });
});
