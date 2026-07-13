// Revalidate every 6 hours, matching the company pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import { listCompanies, MIN_TAG_COMPANY_COUNT } from "@/lib/queries";
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

  // Thin open-vocabulary tags (the LLM emits ~7k, most applying to a single
  // company) make near-duplicate doorway pages. Tags below MIN_TAG_COMPANY_COUNT
  // are already kept out of the sitemap (listAllTags is pre-de-thinned); mirror
  // that here so the reachable page (linked from each /c/[slug]) self-noindexes
  // too — noindex,follow lets the crawler still reach the linked companies.
  // `total` comes from the same count:"exact" query the listing uses; limit:1
  // keeps the payload to a single row. Missing Supabase env → total 0 → noindex,
  // the safe default for a build without secrets.
  const { total } = await listCompanies({ tag, limit: 1 });
  const thin = total < MIN_TAG_COMPANY_COUNT;

  return {
    title: `Tagged ${tag}`,
    description: `US software startups tagged "${tag}", discovered by nous from VC portfolios and funding news.`,
    alternates: { canonical: `/tag/${encodeURIComponent(tag)}` },
    ...(thin ? { robots: { index: false, follow: true } } : {}),
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
