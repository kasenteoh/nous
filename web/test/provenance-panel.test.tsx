import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  COMPLETENESS_RICH_THRESHOLD,
  COMPLETENESS_WELL_THRESHOLD,
  ProvenancePanel,
  completenessLabel,
  lastVerified,
} from "@/components/ProvenancePanel";
import { hasRenderableCitations } from "@/components/Sources";
import type { CompanyRow } from "@/lib/types";

// The panel reads only completeness_score + the six freshness stamps off the
// company; build just those (the prop type is a structural subset of CompanyRow).
type PanelCompany = Pick<
  CompanyRow,
  | "completeness_score"
  | "last_enriched_at"
  | "website_resolved_at"
  | "website_fallback_checked_at"
  | "news_checked_at"
  | "website_funding_checked_at"
  | "employee_count_checked_at"
>;

function company(overrides: Partial<PanelCompany> = {}): PanelCompany {
  return {
    completeness_score: null,
    last_enriched_at: null,
    website_resolved_at: null,
    website_fallback_checked_at: null,
    news_checked_at: null,
    website_funding_checked_at: null,
    employee_count_checked_at: null,
    ...overrides,
  };
}

// ─── completenessLabel thresholds ─────────────────────────────────────────────

describe("completenessLabel", () => {
  it("labels scores at/above the rich threshold 'Richly documented'", () => {
    expect(completenessLabel(0.8)).toBe("Richly documented");
    // Exactly at the boundary is inclusive.
    expect(completenessLabel(COMPLETENESS_RICH_THRESHOLD)).toBe(
      "Richly documented",
    );
  });

  it("labels scores in the well band 'Well documented'", () => {
    expect(completenessLabel(0.6)).toBe("Well documented");
    // Exactly at the lower boundary is inclusive; just under rich is still well.
    expect(completenessLabel(COMPLETENESS_WELL_THRESHOLD)).toBe(
      "Well documented",
    );
    expect(completenessLabel(COMPLETENESS_RICH_THRESHOLD - 0.001)).toBe(
      "Well documented",
    );
  });

  it("returns no label below the well threshold or for null/undefined (positive-only)", () => {
    expect(completenessLabel(0.4)).toBeNull();
    expect(completenessLabel(COMPLETENESS_WELL_THRESHOLD - 0.001)).toBeNull();
    expect(completenessLabel(0)).toBeNull();
    expect(completenessLabel(null)).toBeNull();
    expect(completenessLabel(undefined)).toBeNull();
  });
});

// ─── lastVerified (read-time MAX of the freshness stamps + N days) ────────────

describe("lastVerified", () => {
  const now = new Date("2026-07-14T00:00:00Z");

  it("takes the MAX over the present stamps and floors the whole-day gap", () => {
    const result = lastVerified(
      company({
        last_enriched_at: "2026-07-01T00:00:00Z",
        news_checked_at: "2026-07-10T12:00:00Z", // newest present
        employee_count_checked_at: "2026-06-20T00:00:00Z",
      }),
      now,
    );
    expect(result).not.toBeNull();
    expect(result?.iso).toBe("2026-07-10T12:00:00Z");
    // 2026-07-10T12:00Z → 2026-07-14T00:00Z is 3.5 days → floors to 3.
    expect(result?.days).toBe(3);
  });

  it("considers every stamp, including ones other than last_enriched_at", () => {
    const result = lastVerified(
      company({ website_funding_checked_at: "2026-07-13T00:00:00Z" }),
      now,
    );
    expect(result?.iso).toBe("2026-07-13T00:00:00Z");
    expect(result?.days).toBe(1);
  });

  it("returns null when none of the stamps is present (nothing to claim)", () => {
    expect(lastVerified(company(), now)).toBeNull();
  });

  it("floors a same-day / future stamp at 0 days rather than going negative", () => {
    const result = lastVerified(
      company({ last_enriched_at: "2026-07-14T06:00:00Z" }), // after `now`
      now,
    );
    expect(result?.days).toBe(0);
  });
});

// ─── ProvenancePanel render ───────────────────────────────────────────────────

describe("ProvenancePanel", () => {
  it("renders the badge, freshness line, and sourcing line when all apply", () => {
    render(
      <ProvenancePanel
        company={company({
          completeness_score: 0.9,
          last_enriched_at: "2020-01-01T00:00:00Z",
        })}
        hasSources
      />,
    );

    expect(screen.getByText("Data & provenance")).toBeInTheDocument();
    expect(screen.getByText(/Richly documented/)).toBeInTheDocument();
    expect(screen.getByText(/Last verified/)).toBeInTheDocument();
    // The sourcing line anchors down to the Sources section.
    expect(
      screen.getByRole("link", { name: "recorded source" }),
    ).toHaveAttribute("href", "#sources");
  });

  it("shows a positive badge only — no badge below threshold", () => {
    // Scope each assertion to its own render container (multiple renders share
    // the global document body, so `within` keeps them from colliding).
    const rich = render(
      <ProvenancePanel
        company={company({ completeness_score: 0.8 })}
        hasSources={false}
      />,
    );
    expect(within(rich.container).getByText(/Richly documented/)).toBeInTheDocument();

    const well = render(
      <ProvenancePanel
        company={company({ completeness_score: 0.6 })}
        hasSources={false}
      />,
    );
    expect(within(well.container).getByText(/Well documented/)).toBeInTheDocument();

    // A thin company gets no badge, ever (positive-only) — but the sourcing line
    // still renders, so the panel is not empty.
    const thin = render(
      <ProvenancePanel
        company={company({ completeness_score: 0.4 })}
        hasSources
      />,
    );
    expect(
      within(thin.container).queryByText(/documented/),
    ).not.toBeInTheDocument();
    expect(
      within(thin.container).getByRole("link", { name: "recorded source" }),
    ).toBeInTheDocument();
  });

  it("omits the freshness line when no stamp is present", () => {
    render(
      <ProvenancePanel
        company={company({ completeness_score: 0.9 })}
        hasSources={false}
      />,
    );
    expect(screen.getByText(/Richly documented/)).toBeInTheDocument();
    expect(screen.queryByText(/Last verified/)).not.toBeInTheDocument();
  });

  it("hides the sourcing line when the company has no recorded sources", () => {
    render(
      <ProvenancePanel
        company={company({ completeness_score: 0.9 })}
        hasSources={false}
      />,
    );
    expect(
      screen.queryByRole("link", { name: "recorded source" }),
    ).not.toBeInTheDocument();
  });

  it("renders nothing when none of badge / freshness / sourcing applies (omit-when-empty)", () => {
    const { container } = render(
      <ProvenancePanel
        company={company({ completeness_score: 0.4 })}
        hasSources={false}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("exposes the exact last-verified date in the title attribute", () => {
    render(
      <ProvenancePanel
        company={company({ last_enriched_at: "2026-05-12T00:00:00Z" })}
        hasSources={false}
      />,
    );
    expect(screen.getByText(/Last verified/)).toHaveAttribute(
      "title",
      "May 12, 2026",
    );
  });
});

// ─── hasSources gate parity with <Sources> ────────────────────────────────────
// The sourcing line / #sources anchor must appear iff <Sources> actually renders
// a section — otherwise the anchor is dead and the "every figure links to a
// recorded source" claim is false. Both gate on this SAME predicate.

describe("hasRenderableCitations (the sourcing-line gate)", () => {
  it("is true when at least one citation URL parses to a hostname", () => {
    expect(
      hasRenderableCitations([{ url: "https://techcrunch.com/x" }]),
    ).toBe(true);
    // Mixed: one good URL among unparseable ones still renders a Sources row.
    expect(
      hasRenderableCitations([{ url: "acme.com" }, { url: "https://sec.gov/y" }]),
    ).toBe(true);
  });

  it("is false when EVERY citation URL is unparseable (scheme-less bare domain)", () => {
    // The pipeline stores scheme-less website values like 'acme.com' (the
    // total_raised / leadership source fallback), and `new URL('acme.com')`
    // throws — <Sources> drops them and renders nothing, so the panel must not
    // show a sourcing line pointing at a #sources anchor that won't exist.
    expect(hasRenderableCitations([{ url: "acme.com" }])).toBe(false);
    expect(
      hasRenderableCitations([{ url: "acme.com" }, { url: "not a url" }]),
    ).toBe(false);
  });

  it("is false for no citations", () => {
    expect(hasRenderableCitations([])).toBe(false);
  });
});
