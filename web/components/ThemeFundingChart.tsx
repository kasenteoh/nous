// Server component — funding-by-quarter bars on /themes/[slug]. No "use
// client", no chart library: the page is read-only display, so the chart is
// a hand-rolled inline SVG rendered on the server (zero client JS), themed
// through currentColor so it follows the site's light/dark tokens.
//
// Accessibility: the <svg> is role="img" with an aria-label summarizing the
// series, and every bar carries a <title> (hover/AT per-bar detail) plus
// visible quarter + amount text labels — the numbers are readable without
// color or hover.

import { formatUsd } from "@/lib/format";
import type { QuarterBucket } from "@/lib/funding";

interface Props {
  buckets: QuarterBucket[];
}

// Fixed internal coordinate system; the SVG scales responsively via viewBox
// + width:100%.
const CHART_WIDTH = 720;
const CHART_HEIGHT = 200;
const BAR_AREA_TOP = 24; // room for the amount label above the tallest bar
const BAR_AREA_BOTTOM = CHART_HEIGHT - 28; // room for the quarter labels
const BAR_GAP = 12;

export function ThemeFundingChart({ buckets }: Props) {
  const max = Math.max(...buckets.map((b) => b.totalUsd), 0);
  if (buckets.length === 0 || max === 0) {
    return (
      <p className="text-sm text-ink-muted">
        No dated funding recorded in the last {buckets.length || 8} quarters.
      </p>
    );
  }

  const barWidth =
    (CHART_WIDTH - BAR_GAP * (buckets.length + 1)) / buckets.length;
  const areaHeight = BAR_AREA_BOTTOM - BAR_AREA_TOP;
  const summary = buckets
    .map((b) => `${b.label}: ${formatUsd(b.totalUsd)}`)
    .join(", ");

  return (
    <svg
      viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
      className="w-full"
      role="img"
      aria-label={`Funding raised per quarter. ${summary}.`}
    >
      {/* Baseline */}
      <line
        x1={0}
        y1={BAR_AREA_BOTTOM}
        x2={CHART_WIDTH}
        y2={BAR_AREA_BOTTOM}
        className="stroke-edge"
        strokeWidth={1}
      />
      {buckets.map((bucket, i) => {
        const x = BAR_GAP + i * (barWidth + BAR_GAP);
        // Non-zero quarters always get a visible sliver (≥2px) so a small
        // raise next to a mega-round doesn't disappear.
        const height =
          bucket.totalUsd === 0
            ? 0
            : Math.max(2, (bucket.totalUsd / max) * areaHeight);
        const y = BAR_AREA_BOTTOM - height;
        return (
          <g key={bucket.start}>
            <rect
              x={x}
              y={y}
              width={barWidth}
              height={height}
              rx={2}
              className="fill-current text-money"
            >
              <title>{`${bucket.label}: ${formatUsd(bucket.totalUsd)}`}</title>
            </rect>
            {/* Amount above the bar (— for zero quarters, muted). */}
            <text
              x={x + barWidth / 2}
              y={y - 6}
              textAnchor="middle"
              fontSize={11}
              className={
                bucket.totalUsd === 0
                  ? "fill-current text-ink-faint font-mono"
                  : "fill-current text-money font-mono"
              }
            >
              {bucket.totalUsd === 0 ? "—" : formatUsd(bucket.totalUsd)}
            </text>
            {/* Quarter label below the baseline. */}
            <text
              x={x + barWidth / 2}
              y={BAR_AREA_BOTTOM + 18}
              textAnchor="middle"
              fontSize={11}
              className="fill-current text-ink-muted font-mono"
            >
              {bucket.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
