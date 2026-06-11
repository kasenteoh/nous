// /surprise — land on one random company profile (spec §1). Must never be
// cached: a fresh random pick on every visit.
export const dynamic = "force-dynamic";

import { redirect } from "next/navigation";
import { getRandomCompanySlug } from "@/lib/queries";

export async function GET(): Promise<never> {
  const slug = await getRandomCompanySlug();
  // Empty index (or unconfigured Supabase) — browsing is the next best thing.
  redirect(slug ? `/c/${slug}` : "/companies");
}
