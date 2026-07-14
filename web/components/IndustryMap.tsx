// Server component — the market map for one industry, rendered as a static
// inline SVG (no "use client", no chart library, zero client JS), exactly like
// ThemeFundingChart. The layout math lives in the pure lib/map-layout module;
// this component only renders its output.
//
// Two DELIBERATE deviations from ThemeFundingChart's a11y, because this SVG is
// interactive rather than a read-only chart:
//   - NO role="img". role="img" collapses the subtree for assistive tech, which
//     would hide every node link. Instead the <svg> gets an accessible name via
//     <title id> + aria-labelledby, and each node stays a real focusable link.
//   - Each node is a plain SVG <a href="/c/{slug}"> (SVG2 href), NOT next/link —
//     a full-navigation anchor needs no client runtime, keeping the page static.
//     Each <a> carries a <title> (accessible name + hover tooltip: name + exact
//     latest-round figure).
//
// Colors come from the CSS-var-backed Tailwind tokens (text-accent, text-ink-*,
// border-edge) so they flip with the .dark class automatically. Nodes are
// monochrome accent-green at fillOpacity 0.7 so overlapping PCA dots stay
// visible; category coloring is deferred (see the spec — the palette has no
// honest theme-flipping categorical hues).

import { formatUsd, formatUsdExact } from "@/lib/format";
import { layoutNodes, VIEW_H, VIEW_W, type RawNode } from "@/lib/map-layout";

interface Props {
  group: string;
  nodes: RawNode[];
}

export function IndustryMap({ group, nodes }: Props) {
  if (nodes.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
        <p className="text-ink-muted">The map for {group} is being computed.</p>
        <p className="mt-2 text-sm text-ink-muted">
          Positions are generated from company descriptions; check back soon.
        </p>
      </div>
    );
  }

  const placed = layoutNodes(nodes);
  const titleId = "industry-map-title";

  return (
    <>
      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        className="w-full"
        aria-labelledby={titleId}
      >
        <title id={titleId}>
          {`Market map of ${nodes.length} ${group} ${
            nodes.length === 1 ? "company" : "companies"
          }, positioned by similarity and sized by latest funding.`}
        </title>
        {placed.map((p) => (
          <a key={p.slug} href={`/c/${p.slug}`}>
            <title>
              {`${p.name} — ${formatUsdExact(p.latest_round_amount)} latest round`}
            </title>
            <circle
              cx={p.cx}
              cy={p.cy}
              r={p.r}
              className="fill-current text-accent"
              style={{ fillOpacity: 0.7 }}
            />
            {p.labeled && (
              <text
                x={p.cx + p.r + 3}
                y={p.cy}
                dominantBaseline="middle"
                fontSize={11}
                className="fill-current text-ink-muted font-mono"
              >
                {p.name}
              </text>
            )}
          </a>
        ))}
      </svg>
      {/* Robust AT / no-CSS fallback: the same links as a plain list. Screen
          readers get a navigable index even where SVG <a> support is spotty. */}
      <ul className="sr-only">
        {placed.map((p) => (
          <li key={p.slug}>
            <a href={`/c/${p.slug}`}>
              {p.name} — {formatUsd(p.latest_round_amount)}
            </a>
          </li>
        ))}
      </ul>
    </>
  );
}
