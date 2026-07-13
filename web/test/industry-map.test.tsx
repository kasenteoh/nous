import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { IndustryMap } from "@/components/IndustryMap";
import type { MapCompanyNode } from "@/lib/queries";

function mapNode(overrides: Partial<MapCompanyNode> = {}): MapCompanyNode {
  return {
    slug: overrides.slug ?? "co",
    name: overrides.name ?? "Co",
    map_x: overrides.map_x ?? 0,
    map_y: overrides.map_y ?? 0,
    latest_round_amount: overrides.latest_round_amount ?? null,
    primary_category: overrides.primary_category ?? null,
  };
}

describe("IndustryMap", () => {
  it("renders the empty state (and no svg) when there are no nodes", () => {
    const { container } = render(<IndustryMap group="Fintech" nodes={[]} />);
    expect(
      screen.getByText("The map for Fintech is being computed."),
    ).toBeInTheDocument();
    expect(container.querySelector("svg")).toBeNull();
  });

  it("renders one node anchor per company inside the svg, each linking to /c/[slug]", () => {
    const nodes = [
      mapNode({ slug: "alpha", name: "Alpha", map_x: 0, map_y: 0, latest_round_amount: 5_000_000 }),
      mapNode({ slug: "bravo", name: "Bravo", map_x: 10, map_y: 10, latest_round_amount: 1_000_000 }),
    ];
    const { container } = render(
      <IndustryMap group="Fintech" nodes={nodes} />,
    );

    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    const svgAnchors = svg?.querySelectorAll("a") ?? [];
    expect(svgAnchors).toHaveLength(2);
    expect(svgAnchors[0].getAttribute("href")).toBe("/c/alpha");
    expect(svgAnchors[1].getAttribute("href")).toBe("/c/bravo");
  });

  it("gives the svg an accessible name via <title> + aria-labelledby, and NOT role=img", () => {
    const nodes = [mapNode({ slug: "alpha", name: "Alpha" })];
    const { container } = render(
      <IndustryMap group="Fintech" nodes={nodes} />,
    );
    const svg = container.querySelector("svg");

    // No role="img" — that would collapse the subtree and hide the node links.
    expect(svg?.getAttribute("role")).toBeNull();

    // The accessible name is wired through aria-labelledby → a <title id>.
    const labelledBy = svg?.getAttribute("aria-labelledby");
    expect(labelledBy).toBeTruthy();
    const title = svg?.querySelector(`title#${labelledBy}`);
    expect(title?.textContent).toMatch(/Market map of 1 Fintech company/);
  });

  it("labels the top-funded node with visible text", () => {
    const nodes = [
      mapNode({ slug: "alpha", name: "Alpha", map_x: 0, map_y: 0, latest_round_amount: 9_000_000 }),
      mapNode({ slug: "bravo", name: "Bravo", map_x: 10, map_y: 10, latest_round_amount: 1_000_000 }),
    ];
    const { container } = render(
      <IndustryMap group="Fintech" nodes={nodes} />,
    );
    const svgTexts = Array.from(
      container.querySelectorAll("svg text"),
    ).map((t) => t.textContent);
    expect(svgTexts).toContain("Alpha");
  });

  it("lists every company in the sr-only fallback list", () => {
    const nodes = [
      mapNode({ slug: "alpha", name: "Alpha", latest_round_amount: 5_000_000 }),
      mapNode({ slug: "bravo", name: "Bravo", latest_round_amount: null }),
      mapNode({ slug: "charlie", name: "Charlie", latest_round_amount: 2_000_000 }),
    ];
    const { container } = render(
      <IndustryMap group="Fintech" nodes={nodes} />,
    );
    const list = container.querySelector("ul.sr-only");
    expect(list).not.toBeNull();
    const items = list?.querySelectorAll("li a") ?? [];
    expect(items).toHaveLength(3);
    const hrefs = Array.from(items).map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(["/c/alpha", "/c/bravo", "/c/charlie"]);
  });
});
