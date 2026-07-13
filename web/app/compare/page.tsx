// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { getCompaniesForCompare } from "@/lib/queries";
import { CompareTable } from "@/components/CompareTable";

export const metadata: Metadata = {
  title: "Compare companies",
  description:
    "Compare US software startups side by side — funding, headcount, investors, and competitors.",
  // The slug set is user-driven and infinite; keep crawlers on the canonical.
  alternates: { canonical: "/compare" },
};

// /compare?slugs=a,b,c — 2 to 4 companies.
const MIN_COMPARE = 2;
const MAX_COMPARE = 4;

type SearchParams = { slugs?: string | string[] };

function firstStr(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

/** Parse the comma-separated slug list, trimmed/deduped, capped at MAX_COMPARE. */
function parseSlugs(raw: string): string[] {
  const seen = new Set<string>();
  for (const part of raw.split(",")) {
    const s = part.trim();
    if (s) seen.add(s);
    if (seen.size >= MAX_COMPARE) break;
  }
  return [...seen];
}

export default async function ComparePage({
  searchParams,
}: {
  // Next.js 16: searchParams is a Promise and must be awaited.
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const slugs = parseSlugs(firstStr(sp.slugs));
  const companies =
    slugs.length >= 1 ? await getCompaniesForCompare(slugs) : [];

  const enough = companies.length >= MIN_COMPARE;

  return (
    <main className="flex-1 px-6 py-12 max-w-6xl mx-auto w-full">
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          Compare companies
        </h1>
        <p className="mt-3 text-ink-muted max-w-2xl">
          Side-by-side funding, headcount, investors, and competitors. Add 2–4
          companies via{" "}
          <code className="rounded bg-edge/40 px-1.5 py-0.5 text-sm">
            /compare?slugs=acme,globex
          </code>
          .
        </p>
      </header>

      {!enough ? (
        <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
          <p className="text-ink-muted">
            {slugs.length === 0
              ? "No companies selected to compare."
              : companies.length === 0
                ? "None of those companies are listed."
                : "Pick at least two listed companies to compare."}
          </p>
          <Link
            href="/companies"
            className="mt-4 inline-block text-sm text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent"
          >
            Browse companies →
          </Link>
        </div>
      ) : (
        <CompareTable companies={companies} />
      )}

      <div className="mt-10">
        <Link
          href="/companies"
          className="text-sm font-medium text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
        >
          ← Browse all companies
        </Link>
      </div>
    </main>
  );
}
