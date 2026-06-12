// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { listInvestors } from "@/lib/queries";

export const metadata: Metadata = {
  // The layout's title template appends " — nous".
  title: "Investors",
  description:
    "Every VC firm and investor indexed by nous, ranked by portfolio size.",
  // Paramless self-canonical: ?page= URLs collapse to /investors.
  alternates: { canonical: "/investors" },
};

const PAGE_SIZE = 50;

type SearchParams = {
  page?: string | string[];
};

function firstStr(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

export default async function InvestorsPage({
  searchParams,
}: {
  // Next.js 16: searchParams is a Promise and must be awaited.
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const page = Math.max(1, Number.parseInt(firstStr(sp.page), 10) || 1);
  const offset = (page - 1) * PAGE_SIZE;

  const { rows: investors, total } = await listInvestors({
    limit: PAGE_SIZE,
    offset,
  });

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const firstShown = total === 0 ? 0 : offset + 1;
  const lastShown = Math.min(offset + investors.length, total);

  const hrefForPage = (target: number): string =>
    target > 1 ? `/investors?page=${target}` : "/investors";

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          Investors
        </h1>
        <p className="mt-2 text-sm text-ink-muted">
          {total.toLocaleString("en-US")}{" "}
          {total === 1 ? "firm" : "firms"}, ranked by portfolio size.
        </p>
      </header>

      {/* ── List ────────────────────────────────────────────────────────────── */}
      {investors.length === 0 ? (
        <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
          <p className="text-ink-muted">No investors indexed yet.</p>
        </div>
      ) : (
        <>
          <p className="mb-4 text-sm text-ink-muted">
            Showing {firstShown}–{lastShown} of {total.toLocaleString("en-US")}{" "}
            {total === 1 ? "firm" : "firms"}
          </p>
          <ul className="divide-y divide-edge border-y border-edge">
            {investors.map((inv) => (
              <li key={inv.slug}>
                <Link
                  href={`/investor/${inv.slug}`}
                  className="group flex items-center justify-between gap-4 py-3 hover:bg-edge/30 transition-colors px-2 -mx-2"
                >
                  <span className="font-medium text-ink group-hover:underline underline-offset-2">
                    {inv.name}
                  </span>
                  <span className="shrink-0 text-sm text-ink-muted">
                    {inv.portfolioCount.toLocaleString("en-US")}{" "}
                    {inv.portfolioCount === 1 ? "company" : "companies"}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* ── Pagination ──────────────────────────────────────────────────────── */}
      {totalPages > 1 && (
        <nav
          className="mt-10 flex items-center justify-between"
          aria-label="Pagination"
        >
          {page > 1 ? (
            <Link
              href={hrefForPage(page - 1)}
              rel="prev"
              className="rounded-md border border-edge px-4 py-2 text-sm text-ink-soft hover:border-ink-muted transition-colors"
            >
              ← Previous
            </Link>
          ) : (
            <span className="rounded-md border border-edge px-4 py-2 text-sm text-ink-faint cursor-default">
              ← Previous
            </span>
          )}

          <span className="text-sm text-ink-muted">
            Page {page} of {totalPages}
          </span>

          {page < totalPages ? (
            <Link
              href={hrefForPage(page + 1)}
              rel="next"
              className="rounded-md border border-edge px-4 py-2 text-sm text-ink-soft hover:border-ink-muted transition-colors"
            >
              Next →
            </Link>
          ) : (
            <span className="rounded-md border border-edge px-4 py-2 text-sm text-ink-faint cursor-default">
              Next →
            </span>
          )}
        </nav>
      )}
    </main>
  );
}
