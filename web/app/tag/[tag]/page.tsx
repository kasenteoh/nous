// Revalidate every 6 hours, matching the company pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import {
  FacetListingPage,
  parseFacetSearchParams,
  type FacetSearchParams,
} from "@/components/FacetListingPage";

type Props = {
  params: Promise<{ tag: string }>;
  // Next.js 16: searchParams is a Promise and must be awaited.
  searchParams: Promise<FacetSearchParams>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { tag: rawTag } = await params;
  const tag = decodeURIComponent(rawTag);

  return {
    title: `Tagged ${tag}`,
    description: `US software startups tagged "${tag}", discovered by nous from VC portfolios and funding news.`,
    alternates: { canonical: `/tag/${encodeURIComponent(tag)}` },
  };
}

export default async function TagPage({ params, searchParams }: Props) {
  const [{ tag: rawTag }, sp] = await Promise.all([params, searchParams]);
  const tag = decodeURIComponent(rawTag);
  const { page, sort } = parseFacetSearchParams(sp);

  return (
    <FacetListingPage
      heading={`Tagged “${tag}”`}
      basePath={`/tag/${encodeURIComponent(tag)}`}
      filter={{ tag }}
      page={page}
      sort={sort}
    />
  );
}
