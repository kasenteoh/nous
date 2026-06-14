// Revalidate every 6 hours, matching the company pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import {
  FacetListingPage,
  parseFacetSearchParams,
  type FacetSearchParams,
} from "@/components/FacetListingPage";

type Props = {
  params: Promise<{ state: string }>;
  // Next.js 16: searchParams is a Promise and must be awaited.
  searchParams: Promise<FacetSearchParams>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { state: rawState } = await params;
  // hq_state is stored as the 2-letter uppercase code, so normalize the URL
  // segment to uppercase — otherwise /location/ca 404s while /location/CA works.
  const state = decodeURIComponent(rawState).toUpperCase();

  return {
    title: `Startups in ${state}`,
    description: `US software startups headquartered in ${state}, discovered by nous from VC portfolios and funding news.`,
    alternates: { canonical: `/location/${encodeURIComponent(state)}` },
  };
}

export default async function LocationPage({ params, searchParams }: Props) {
  const [{ state: rawState }, sp] = await Promise.all([params, searchParams]);
  // Match the uppercase code stored in hq_state (see generateMetadata) so a
  // lowercase/mixed-case URL resolves instead of 404-ing.
  const state = decodeURIComponent(rawState).toUpperCase();
  const { page, sort } = parseFacetSearchParams(sp);

  return (
    <FacetListingPage
      heading={`Startups in ${state}`}
      basePath={`/location/${encodeURIComponent(state)}`}
      filter={{ state }}
      page={page}
      sort={sort}
    />
  );
}
