// The /alternatives/[slug] provenance gates (review catch — this was the one
// machine-syndicated surface gate without a test): a describe-fallback
// description is held out of the page's <meta> lead but stays in the visible
// header WITH its attribution rider.

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AlternativesPage, {
  generateMetadata,
} from "@/app/alternatives/[slug]/page";
import { getAliasTargetSlug, getAlternatives } from "@/lib/queries";
import type { AlternativesData } from "@/lib/types";

vi.mock("@/lib/queries", () => ({
  getAliasTargetSlug: vi.fn(),
  getAlternatives: vi.fn(),
}));

function data(
  overrides: Partial<AlternativesData["company"]> = {},
): AlternativesData {
  return {
    company: {
      slug: "acme-robotics",
      name: "Acme Robotics",
      description_short: "Builds humanoid robots.",
      industry_group: "Robotics",
      ...overrides,
    },
    resolved: [],
    named: [],
  };
}

beforeEach(() => {
  vi.mocked(getAliasTargetSlug).mockResolvedValue(null);
});

describe("alternatives page describe-fallback gating", () => {
  it("holds a fallback description out of the meta lead", async () => {
    vi.mocked(getAlternatives).mockResolvedValue(
      data({ description_source: "fallback" }),
    );
    const meta = await generateMetadata({
      params: Promise.resolve({ slug: "acme-robotics" }),
    });
    expect(meta.description).not.toContain("Builds humanoid robots.");
    expect(meta.description).toContain("Acme Robotics");
  });

  it("keeps an own-website description in the meta lead (absent field)", async () => {
    vi.mocked(getAlternatives).mockResolvedValue(data());
    const meta = await generateMetadata({
      params: Promise.resolve({ slug: "acme-robotics" }),
    });
    expect(meta.description).toContain("Builds humanoid robots.");
  });

  it("keeps the visible header description and adds the attribution rider for fallback rows", async () => {
    vi.mocked(getAlternatives).mockResolvedValue(
      data({ description_source: "fallback" }),
    );
    render(
      await AlternativesPage({
        params: Promise.resolve({ slug: "acme-robotics" }),
      }),
    );
    expect(screen.getByText("Builds humanoid robots.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Description written by nous from Wikidata and press coverage",
      ),
    ).toBeInTheDocument();
  });

  it("shows no rider for own-website descriptions", async () => {
    vi.mocked(getAlternatives).mockResolvedValue(data());
    render(
      await AlternativesPage({
        params: Promise.resolve({ slug: "acme-robotics" }),
      }),
    );
    expect(screen.getByText("Builds humanoid robots.")).toBeInTheDocument();
    expect(
      screen.queryByText(
        "Description written by nous from Wikidata and press coverage",
      ),
    ).not.toBeInTheDocument();
  });
});
