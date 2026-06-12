// Per-company OG card (1200×630): name, industry when known, total raised
// when known, small wordmark. Unknown slug (or missing Supabase env) falls
// back to the generic site card — this route must never throw.
// Rendered by Satori via next/og; see lib/og.tsx for the styling constraints.

import { ImageResponse } from "next/og";
import { CompanyOgCard, OG_SIZE, SiteOgCard } from "@/lib/og";
import { getCompanyOgData } from "@/lib/queries";

export const size = OG_SIZE;
export const alt = "Company profile on nous";
export const contentType = "image/png";

export default async function Image({
  params,
}: {
  // Next.js 16: params is a Promise and must be awaited.
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const company = await getCompanyOgData(slug);

  return new ImageResponse(
    company ? (
      <CompanyOgCard
        name={company.name}
        industryGroup={company.industry_group}
        totalRaised={company.totalRaised}
      />
    ) : (
      <SiteOgCard />
    ),
    size,
  );
}
