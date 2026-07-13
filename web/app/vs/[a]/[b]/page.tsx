// /vs/[a]/[b] — head-to-head comparison of two companies, the "{A} vs {B}"
// search surface. Renders the shared CompareTable for any two LISTED companies,
// but indexing is conservative: a page is indexable ONLY when the two are a
// resolved competitor edge AND at least one side has real funding — otherwise it
// noindexes (the long tail of arbitrary pairs would be thin, near-duplicate
// doorway pages). A pair is unordered, so both URL orderings render identical
// content and point their canonical at the lexicographically-sorted URL.
//
// Discovery of the indexable pairs is by internal links from /alternatives/
// [slug] (each resolved competitor links here), not a giant pair-sitemap.

// Revalidate every 6 hours, matching the company pages' ISR window.
export const revalidate = 21600;

import { cache } from "react";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { CompareTable } from "@/components/CompareTable";
import { areCompetitorsBySlug, getCompaniesForCompare } from "@/lib/queries";
import { canonicalVsPair, vsPath } from "@/lib/vs";
import type { CompareCompany } from "@/lib/types";

type Props = {
  params: Promise<{ a: string; b: string }>;
};

interface VsData {
  /** Exactly two companies, in canonical (lexicographic slug) order. */
  companies: CompareCompany[];
  /** Resolved competitor edge in either direction. */
  isCompetitorEdge: boolean;
  /** At least one side has recorded funding (a stated total or ≥1 round). */
  hasFunding: boolean;
  /** Canonical (sorted) slugs — the canonical URL uses these. */
  a: string;
  b: string;
}

/** True when a company has any recorded funding to compare on. */
function hasFundingSignal(c: CompareCompany): boolean {
  return (c.totalRaised ?? 0) > 0 || c.roundCount > 0;
}

/**
 * Everything the page + its metadata render, in canonical order, or null when
 * the pair is invalid (same company, or fewer than two LISTED companies → 404).
 * Called by BOTH generateMetadata and the page; `cache()` dedupes the pair of
 * calls within a single render pass so the Supabase queries run once, not twice.
 * Each render is additionally ISR-cached for 6h.
 */
const loadVs = cache(
  async (rawA: string, rawB: string): Promise<VsData | null> => {
    const [a, b] = canonicalVsPair(rawA, rawB);
    if (a === b) return null; // comparing a company with itself is meaningless

    const [companies, isCompetitorEdge] = await Promise.all([
      // Fetch in canonical order — getCompaniesForCompare preserves it and drops
      // any excluded/unknown slug, so length < 2 means one side isn't listed.
      getCompaniesForCompare([a, b]),
      areCompetitorsBySlug(a, b),
    ]);
    if (companies.length < 2) return null;

    return {
      companies,
      isCompetitorEdge,
      hasFunding: companies.some(hasFundingSignal),
      a,
      b,
    };
  },
);

// ─── Metadata ─────────────────────────────────────────────────────────────────

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { a: rawA, b: rawB } = await params;
  const data = await loadVs(rawA, rawB);
  if (!data) {
    // The layout's title template appends " — nous".
    return { title: "Comparison not found" };
  }
  const [first, second] = data.companies;
  // Conservative indexing: only a funded competitor edge is worth indexing;
  // every other pair renders but stays out of the index (follow, so the linked
  // company pages still get crawled).
  const indexable = data.isCompetitorEdge && data.hasFunding;
  return {
    title: `${first.name} vs ${second.name}`,
    description: `Compare ${first.name} and ${second.name} side by side — funding, headcount, investors, and competitors, tracked by nous.`,
    alternates: { canonical: vsPath(data.a, data.b) },
    ...(indexable ? {} : { robots: { index: false, follow: true } }),
  };
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function VsPage({ params }: Props) {
  const { a: rawA, b: rawB } = await params;
  const data = await loadVs(rawA, rawB);
  if (!data) notFound();

  const [first, second] = data.companies;
  const industries = [first.industryGroup, second.industryGroup].filter(
    (g): g is string => Boolean(g),
  );
  const sharedIndustry =
    industries.length === 2 && industries[0] === industries[1]
      ? industries[0]
      : null;

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          {first.name} <span className="text-ink-faint">vs</span> {second.name}
        </h1>
        <p className="mt-3 text-sm text-ink-muted max-w-2xl">
          {data.isCompetitorEdge
            ? `${first.name} and ${second.name} are competitors`
            : `${first.name} and ${second.name}`}
          {sharedIndustry ? ` in ${sharedIndustry}` : ""} — funding, headcount,
          investors, and competitors side by side, from recorded data.
        </p>
      </header>

      <CompareTable companies={data.companies} />

      {/* ── Footer links ────────────────────────────────────────────────────── */}
      <div className="mt-10 flex flex-wrap gap-x-6 gap-y-2 text-sm">
        <Link
          href={`/c/${first.slug}`}
          className="font-medium text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
        >
          {first.name} profile →
        </Link>
        <Link
          href={`/c/${second.slug}`}
          className="font-medium text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
        >
          {second.name} profile →
        </Link>
        <Link
          href="/companies"
          className="text-ink-muted underline underline-offset-4 decoration-edge hover:text-ink transition-colors"
        >
          Browse all companies
        </Link>
      </div>
    </main>
  );
}
