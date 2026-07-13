// Pure geometry for the market map SVG. No DB, no React, no `server-only`
// (import-safe & unit-testable anywhere, exactly like industry.ts / funding.ts):
// it takes MapCompanyNode-shaped data + a fixed viewBox and returns
// positioned/sized nodes, so the layout math is unit-tested in isolation and
// IndustryMap.tsx just renders the output. Every function is pure and
// deterministic.

/** Input node: precomputed PCA coords + the funding used to size the dot. */
export interface RawNode {
  slug: string;
  name: string;
  map_x: number;
  map_y: number;
  latest_round_amount: number | null;
}

/** A node placed in the viewBox: screen coords, radius, and whether it earned
 *  a text label. */
export interface PlacedNode extends RawNode {
  cx: number;
  cy: number;
  r: number;
  labeled: boolean;
}

// Internal viewBox coordinate system; the SVG scales responsively via
// viewBox + width:100%, so these are unitless design pixels, not CSS pixels.
export const VIEW_W = 960;
export const VIEW_H = 600;
export const PAD = 40; // keep dots + labels off the edges
export const R_MIN = 3;
export const R_MAX = 22;
export const MAX_LABELS = 16; // clutter guard

// Approximate metrics for the 11px mono label, used to reserve non-overlapping
// label boxes. CHAR_W is a conservative average glyph advance; LABEL_H the line
// box. Both are intentionally generous so the greedy packer errs toward fewer,
// cleanly separated labels rather than crowding.
const CHAR_W = 6;
const LABEL_H = 13;
const LABEL_MAX_CHARS = 22; // cap the reserved width for very long names

/**
 * Normalize one axis value into [lo, hi]. A degenerate range (all values equal,
 * or a single node) collapses to the midpoint, so nothing divides by zero and a
 * lone/clustered set lands centered rather than jammed into a corner.
 */
export function scaleAxis(
  v: number,
  min: number,
  max: number,
  lo: number,
  hi: number,
): number {
  if (max - min < 1e-9) return (lo + hi) / 2;
  return lo + ((v - min) / (max - min)) * (hi - lo);
}

/**
 * sqrt-scaled radius so a node's AREA is ~proportional to its funding
 * (radius ∝ √funding). null / 0 / negative amount, or a non-positive
 * maxAmount, → R_MIN. Amounts above maxAmount clamp to R_MAX.
 */
export function fundingRadius(
  amount: number | null,
  maxAmount: number,
): number {
  if (!amount || amount <= 0 || maxAmount <= 0) return R_MIN;
  return (
    R_MIN + (R_MAX - R_MIN) * Math.sqrt(Math.min(amount, maxAmount) / maxAmount)
  );
}

interface LabelBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

function overlaps(a: LabelBox, b: LabelBox): boolean {
  return !(a.x1 < b.x0 || a.x0 > b.x1 || a.y1 < b.y0 || a.y0 > b.y1);
}

/**
 * Position + size every node in the viewBox and pick which get a text label.
 *
 * - coords: min/max over the set map each axis independently onto
 *   [PAD, VIEW-PAD]. PCA axes are unitless, so a per-axis fit uses the whole
 *   canvas (the two axes carry no shared scale worth preserving).
 * - radius: fundingRadius against the max latest raise in the set.
 * - labels: the input is pre-sorted funding-desc by the query, so we walk that
 *   order and label greedily, skipping any node whose label box would overlap
 *   an already-placed label box (simple AABB test) — biggest-money names win
 *   the scarce space. Capped at MAX_LABELS.
 *
 * Empty input → []. Deterministic for a given input order.
 */
export function layoutNodes(nodes: readonly RawNode[]): PlacedNode[] {
  if (nodes.length === 0) return [];

  const xs = nodes.map((n) => n.map_x);
  const ys = nodes.map((n) => n.map_y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const maxAmt = Math.max(0, ...nodes.map((n) => n.latest_round_amount ?? 0));

  const placed: PlacedNode[] = nodes.map((n) => ({
    ...n,
    cx: scaleAxis(n.map_x, minX, maxX, PAD, VIEW_W - PAD),
    cy: scaleAxis(n.map_y, minY, maxY, PAD, VIEW_H - PAD),
    r: fundingRadius(n.latest_round_amount, maxAmt),
    labeled: false,
  }));

  // Greedy non-overlapping labels, in the query's funding-desc order.
  const boxes: LabelBox[] = [];
  let count = 0;
  for (const p of placed) {
    if (count >= MAX_LABELS) break;
    const w = Math.min(p.name.length, LABEL_MAX_CHARS) * CHAR_W;
    const x0 = p.cx + p.r + 3;
    const y0 = p.cy - LABEL_H / 2;
    const box: LabelBox = { x0, y0, x1: x0 + w, y1: y0 + LABEL_H };
    if (boxes.some((b) => overlaps(box, b))) continue;
    boxes.push(box);
    p.labeled = true;
    count++;
  }

  return placed;
}
