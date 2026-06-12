// Shared building blocks for the OG-image routes (app/opengraph-image.tsx and
// app/c/[slug]/opengraph-image.tsx).
//
// This JSX is rendered by Satori (next/og's ImageResponse), not the DOM:
// Tailwind classes do not exist there, so styling must be inline style
// objects — the project's "Tailwind for all styling" rule applies to DOM
// components only. Satori also requires an explicit `display: "flex"` on any
// element with more than one child. Fonts: ImageResponse ships a bundled
// default sans-serif; we deliberately load no font files.

import { formatUsd } from "@/lib/format";

export const OG_SIZE = { width: 1200, height: 630 };

// Dark terminal palette — mirrors the dark-theme tokens in app/globals.css.
const C = {
  canvas: "#0a0a0a",
  ink: "#e4e4e4",
  inkSoft: "#9a9a9a",
  inkMuted: "#5f5f5f",
  edge: "#2a2a2a",
  accent: "#7ee787",
  money: "#a5f4ae",
} as const;

/** Site-wide card: big wordmark + tagline. Also the fallback for unknown companies. */
export function SiteOgCard() {
  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: C.canvas,
        border: `2px solid ${C.edge}`,
      }}
    >
      <div style={{ display: "flex", alignItems: "center" }}>
        <div
          style={{
            fontSize: 160,
            fontWeight: 700,
            letterSpacing: "-0.04em",
            color: C.ink,
          }}
        >
          nous
        </div>
        {/* Terminal block cursor. */}
        <div
          style={{
            width: 30,
            height: 112,
            marginLeft: 26,
            backgroundColor: C.accent,
          }}
        />
      </div>
      <div
        style={{
          marginTop: 30,
          fontSize: 36,
          lineHeight: 1.4,
          color: C.inkSoft,
          textAlign: "center",
          maxWidth: 940,
        }}
      >
        US software startup discovery, from VC portfolios and funding news
      </div>
    </div>
  );
}

export interface CompanyOgCardProps {
  name: string;
  industryGroup: string | null;
  /** Sum of known round amounts in USD; the line is omitted unless > 0. */
  totalRaised: number;
}

/** Per-company card: name, industry when known, total raised when known. */
export function CompanyOgCard({
  name,
  industryGroup,
  totalRaised,
}: CompanyOgCardProps) {
  // Step the name's font size down as it gets longer, with overflow-hidden as
  // a backstop so pathological names never escape the card.
  const nameFontSize =
    name.length > 48 ? 52 : name.length > 28 ? 68 : name.length > 16 ? 84 : 100;

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        backgroundColor: C.canvas,
        border: `2px solid ${C.edge}`,
        padding: 72,
      }}
    >
      {/* Small wordmark, top-left. */}
      <div style={{ display: "flex", alignItems: "center" }}>
        <div style={{ fontSize: 40, fontWeight: 700, color: C.accent }}>
          nous
        </div>
        <div
          style={{
            width: 12,
            height: 30,
            marginLeft: 10,
            backgroundColor: C.accent,
          }}
        />
      </div>

      {/* Company name + industry. */}
      <div style={{ display: "flex", flexDirection: "column" }}>
        <div
          style={{
            fontSize: nameFontSize,
            fontWeight: 700,
            letterSpacing: "-0.02em",
            lineHeight: 1.05,
            color: C.ink,
            maxHeight: 320,
            overflow: "hidden",
          }}
        >
          {name}
        </div>
        {industryGroup ? (
          <div style={{ marginTop: 20, fontSize: 34, color: C.inkSoft }}>
            {industryGroup}
          </div>
        ) : null}
      </div>

      {/* Footer: total raised when known; otherwise an empty row so the
          space-between layout keeps the name block centered. */}
      <div style={{ display: "flex", alignItems: "center", minHeight: 44 }}>
        {totalRaised > 0 ? (
          <div style={{ display: "flex", alignItems: "baseline" }}>
            <div style={{ fontSize: 32, color: C.inkSoft }}>Total raised</div>
            <div
              style={{
                marginLeft: 16,
                fontSize: 36,
                fontWeight: 700,
                color: C.money,
              }}
            >
              {formatUsd(totalRaised)}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
