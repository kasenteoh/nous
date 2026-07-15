// Tests for Provenance UI PR 3 — granular per-fact sourcing:
//   1. <SourceLink>       — the subtle inline source superscript (renders only
//                           for a present + parseable http(s) URL).
//   2. citationSourceType — host → source-type label inference (unknown → omit).
//   3. <Sources>          — the muted source-type tag rendered per citation.
//   4. <EventTimeline>    — extraction_confidence tooltip on ALL rounds, visible
//                           pill only for `low`, + the per-round source link.

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SourceLink } from "@/components/SourceLink";
import { Sources, citationSourceType } from "@/components/Sources";
import { EventTimeline } from "@/components/EventTimeline";
import type { FundingRoundWithInvestors } from "@/lib/types";

// ─── Fixtures ─────────────────────────────────────────────────────────────────

let seq = 0;

function round(
  overrides: Partial<FundingRoundWithInvestors> = {},
): FundingRoundWithInvestors {
  seq += 1;
  return {
    id: `round-${seq}`,
    company_id: "c-main",
    round_type: "Series A",
    amount_raised: 15_000_000,
    valuation_post_money: null,
    valuation_source: null,
    announced_date: "2026-03-01",
    primary_news_url: null,
    extraction_confidence: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    leadInvestors: [],
    otherInvestors: [],
    ...overrides,
  };
}

// ─── <SourceLink> ─────────────────────────────────────────────────────────────

describe("SourceLink", () => {
  it("renders an external link for a parseable http(s) URL", () => {
    render(<SourceLink url="https://techcrunch.com/story" label="Total raised" />);
    const link = screen.getByRole("link", {
      name: /Source for Total raised \(techcrunch\.com\)/,
    });
    expect(link).toHaveAttribute("href", "https://techcrunch.com/story");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    // Hover tooltip names the fact + the resolved host.
    expect(link).toHaveAttribute("title", "Total raised — source: techcrunch.com");
  });

  it("renders nothing when the URL is absent", () => {
    const nullUrl = render(<SourceLink url={null} label="Total raised" />);
    expect(nullUrl.container).toBeEmptyDOMElement();
    const undef = render(<SourceLink url={undefined} label="Total raised" />);
    expect(undef.container).toBeEmptyDOMElement();
  });

  it("renders nothing for a scheme-less bare domain (no dead superscript)", () => {
    // The pipeline stores scheme-less fallbacks like 'acme.com'; new URL() throws
    // → the affordance must self-omit rather than link nowhere.
    const { container } = render(<SourceLink url="acme.com" label="Website" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for a non-http(s) scheme", () => {
    const { container } = render(
      <SourceLink url="mailto:hi@acme.com" label="Website" />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for a malformed URL", () => {
    const { container } = render(<SourceLink url="::::" label="Status" />);
    expect(container).toBeEmptyDOMElement();
  });
});

// ─── citationSourceType (pure host → label inference) ─────────────────────────

describe("citationSourceType", () => {
  it("labels wikidata.org (and subdomains) 'Wikidata'", () => {
    expect(citationSourceType("https://www.wikidata.org/wiki/Q42")).toBe(
      "Wikidata",
    );
    expect(citationSourceType("https://m.wikidata.org/wiki/Q42")).toBe(
      "Wikidata",
    );
  });

  it("labels the company's own domain 'Website'", () => {
    expect(
      citationSourceType("https://acme.com/team", { companyHost: "acme.com" }),
    ).toBe("Website");
  });

  it("tolerates a scheme-less / www company website when matching 'Website'", () => {
    // Sources normalizes companyWebsite via tolerantHost, so a bare domain still
    // matches a cited company-domain URL.
    expect(
      citationSourceType("https://acme.com/x", { companyHost: "acme.com" }),
    ).toBe("Website");
  });

  it("labels known press / press-wire hosts 'News' (incl. subdomains)", () => {
    expect(citationSourceType("https://techcrunch.com/a")).toBe("News");
    expect(citationSourceType("https://www.forbes.com/b")).toBe("News");
    expect(citationSourceType("https://feeds.reuters.com/c")).toBe("News");
  });

  it("labels Google News (the dominant funding-source host) 'News'", () => {
    // Funding rounds cite their `primary_news_url`, a Google News RSS link;
    // without this the majority of citations would render untagged.
    expect(
      citationSourceType(
        "https://news.google.com/rss/articles/CBMiP0FVX3lxTE9L?oc=5",
      ),
    ).toBe("News");
    // Only the news subdomain — a bare google.com host stays un-inferable.
    expect(citationSourceType("https://google.com/search?q=acme")).toBeNull();
  });

  it("omits the label for an un-inferable host (never a guess)", () => {
    expect(citationSourceType("https://some-random-blog.example/x")).toBeNull();
    // An unknown host is NOT the company domain, wikidata, or a known press host.
    expect(
      citationSourceType("https://randomhost.io/y", { companyHost: "acme.com" }),
    ).toBeNull();
  });

  it("omits the label for an unparseable URL", () => {
    expect(citationSourceType("not a url")).toBeNull();
  });

  it("omits the label for a non-http(s) scheme (mirrors SourceLink)", () => {
    // new URL('ftp://techcrunch.com/x') parses and yields a hostname, but it is
    // not a real web source — no source-type tag (and no dead label).
    expect(citationSourceType("ftp://techcrunch.com/wire")).toBeNull();
    expect(citationSourceType("ws://techcrunch.com/live")).toBeNull();
  });

  it("uses website_source as ground truth for the website-provenance URL", () => {
    // A VC-portfolio page host isn't otherwise inferable; the website_source
    // enum is the only reliable signal, so it overrides host inference.
    expect(
      citationSourceType("https://sequoiacap.com/companies/acme", {
        websiteSource: "vc_portfolio",
        websiteSourceUrl: "https://sequoiacap.com/companies/acme",
      }),
    ).toBe("VC portfolio");
    // news_outbound / wikidata enum values map too.
    expect(
      citationSourceType("https://obscure-outlet.example/story", {
        websiteSource: "news_outbound",
        websiteSourceUrl: "https://obscure-outlet.example/story",
      }),
    ).toBe("News");
  });

  it("only applies the website_source override to the matching URL", () => {
    // A different citation URL doesn't inherit the website provenance type.
    expect(
      citationSourceType("https://randomhost.io/other", {
        websiteSource: "vc_portfolio",
        websiteSourceUrl: "https://sequoiacap.com/companies/acme",
      }),
    ).toBeNull();
  });
});

// ─── <Sources> source-type tags ───────────────────────────────────────────────

describe("Sources source-type labels", () => {
  it("tags a press citation 'News'", () => {
    render(
      <Sources
        citations={[{ label: "Series A · $15M", url: "https://techcrunch.com/a" }]}
      />,
    );
    expect(screen.getByText("· News")).toBeInTheDocument();
  });

  it("tags a company-domain citation 'Website' when the website is known", () => {
    render(
      <Sources
        citations={[{ label: "Leadership", url: "https://acme.com/team" }]}
        companyWebsite="acme.com"
      />,
    );
    expect(screen.getByText("· Website")).toBeInTheDocument();
  });

  it("uses website_source to tag the website-provenance citation 'VC portfolio'", () => {
    render(
      <Sources
        citations={[
          { label: "Website", url: "https://sequoiacap.com/companies/acme" },
        ]}
        websiteSource="vc_portfolio"
        websiteSourceUrl="https://sequoiacap.com/companies/acme"
      />,
    );
    expect(screen.getByText("· VC portfolio")).toBeInTheDocument();
  });

  it("renders no source-type tag for an un-inferable host", () => {
    render(
      <Sources
        citations={[{ label: "Mystery", url: "https://some-blog.example/x" }]}
      />,
    );
    // The citation still renders (its host link), but with no source-type tag.
    expect(screen.getByText("Mystery")).toBeInTheDocument();
    expect(screen.queryByText(/^· /)).not.toBeInTheDocument();
  });
});

// ─── <EventTimeline> confidence transparency + per-round source link ──────────

describe("EventTimeline confidence transparency", () => {
  function tooltipOf(roundType: string): string | null {
    return screen.getByText(roundType).closest("p")?.getAttribute("title") ?? null;
  }

  it("puts a confidence tooltip on high/medium rounds with NO pill", () => {
    render(
      <EventTimeline
        rounds={[
          round({ round_type: "High round", extraction_confidence: "high" }),
          round({ round_type: "Mid round", extraction_confidence: "medium" }),
        ]}
        news={[]}
      />,
    );
    expect(tooltipOf("High round")).toBe("Extracted with high confidence");
    expect(tooltipOf("Mid round")).toBe("Extracted with medium confidence");
    // No visible pill for high/medium.
    expect(screen.queryByText("low confidence")).not.toBeInTheDocument();
  });

  it("keeps the visible pill for low, plus the tooltip", () => {
    render(
      <EventTimeline
        rounds={[round({ round_type: "Low round", extraction_confidence: "low" })]}
        news={[]}
      />,
    );
    expect(tooltipOf("Low round")).toBe("Extracted with low confidence");
    const pill = screen.getByText("low confidence");
    expect(pill).toBeInTheDocument();
    expect(pill).toHaveAttribute(
      "title",
      "Extracted with low confidence — treat as unverified",
    );
  });

  it("claims no confidence when the enum is absent (null → no tooltip, no pill)", () => {
    render(
      <EventTimeline
        rounds={[round({ round_type: "Unscored", extraction_confidence: null })]}
        news={[]}
      />,
    );
    expect(tooltipOf("Unscored")).toBeNull();
    expect(screen.queryByText("low confidence")).not.toBeInTheDocument();
  });

  it("renders a per-round source link only when primary_news_url is parseable", () => {
    const withUrl = render(
      <EventTimeline
        rounds={[
          round({
            round_type: "Sourced round",
            primary_news_url: "https://techcrunch.com/round",
          }),
        ]}
        news={[]}
      />,
    );
    const link = within(withUrl.container).getByRole("link", {
      name: /Source for Funding round/,
    });
    expect(link).toHaveAttribute("href", "https://techcrunch.com/round");

    // Absent / scheme-less URL → no source superscript.
    const noUrl = render(
      <EventTimeline
        rounds={[round({ round_type: "Unsourced round", primary_news_url: null })]}
        news={[]}
      />,
    );
    expect(
      within(noUrl.container).queryByRole("link", {
        name: /Source for Funding round/,
      }),
    ).not.toBeInTheDocument();
  });
});
