// Site-wide OG card (1200×630) — wordmark + tagline in the terminal palette.
// Rendered by Satori via next/og; see lib/og.tsx for the styling constraints.

import { ImageResponse } from "next/og";
import { OG_SIZE, SiteOgCard } from "@/lib/og";

export const size = OG_SIZE;
export const alt =
  "nous — US software startup discovery, from VC portfolios and funding news";
export const contentType = "image/png";

export default function Image() {
  return new ImageResponse(<SiteOgCard />, size);
}
