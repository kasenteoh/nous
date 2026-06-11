// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import Link from "next/link";
import {
  listCompanies,
  listIndustryGroups,
  type CompanyListSort,
} from "@/lib/queries";
import { formatLocation } from "@/lib/format";

const PAGE_SIZE = 30;

const SORT_OPTIONS: { value: CompanyListSort; label: string }[] = [
  { value: "name_asc", label: "Name (A–Z)" },
  { value: "name_desc", label: "Name (Z–A)" },
  { value: "recent", label: "Recently added" },
];

// discovered_via is a small fixed enum (see pipeline auto_create_company), so
// the filter options are hardcoded rather than queried.
const SOURCE_OPTIONS: { value: string; label: string }[] = [
  { value: "vc_portfolio", label: "VC portfolio" },
  { value: "news", label: "News" },
  { value: "techcrunch", label: "TechCrunch" },
  { value: "unknown", label: "Unknown" },
];

type SearchParams = {
  q?: string | string[];
  industry?: string | string[];
  source?: string | string[];
  sort?: string | string[];
  page?: string | string[];
};

function firstStr(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

export default async function Home({
  searchParams,
}: {
  // Next.js 16: searchParams is a Promise and must be awaited.
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const q = firstStr(sp.q).trim();
  const industry = firstStr(sp.industry);
  const source = firstStr(sp.source);
  const sortRaw = firstStr(sp.sort);
  const sort: CompanyListSort = SORT_OPTIONS.some((o) => o.value === sortRaw)
    ? (sortRaw as CompanyListSort)
    : "name_asc";
  const page = Math.max(1, Number.parseInt(firstStr(sp.page), 10) || 1);
  const offset = (page - 1) * PAGE_SIZE;

  const [{ rows: companies, total }, industries] = await Promise.all([
    listCompanies({
      search: q || undefined,
      industry_group: industry || undefined,
      discovered_via: source || undefined,
      sort,
      limit: PAGE_SIZE,
      offset,
    }),
    listIndustryGroups(),
  ]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const hasFilters = Boolean(q || industry || source);
  const firstShown = total === 0 ? 0 : offset + 1;
  const lastShown = Math.min(offset + companies.length, total);

  // Build a /?… href for a target page, preserving the active filters/sort.
  const hrefForPage = (target: number): string => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (industry) params.set("industry", industry);
    if (source) params.set("source", source);
    if (sort !== "name_asc") params.set("sort", sort);
    if (target > 1) params.set("page", String(target));
    const qs = params.toString();
    return qs ? `/?${qs}` : "/";
  };

  const selectClass =
    "rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-3 py-2 text-sm text-zinc-700 dark:text-zinc-300 focus:outline-none focus:ring-2 focus:ring-zinc-400 dark:focus:ring-zinc-600";

  return (
    <main className="flex-1 px-6 py-12 max-w-6xl mx-auto w-full">
      {/* ── Hero ──────────────────────────────────────────────────────────── */}
      <header className="mb-8">
        <h1 className="text-5xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          nous
        </h1>
        <p className="mt-3 text-lg text-zinc-500 dark:text-zinc-400 max-w-xl">
          US software startups, discovered from VC portfolios and funding news.
        </p>
      </header>

      {/* ── Search + filters (GET form — URL-driven, no client JS) ─────────── */}
      <form
        method="GET"
        action="/"
        className="mb-6 flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center"
      >
        <input
          type="search"
          name="q"
          defaultValue={q}
          placeholder="Search companies…"
          aria-label="Search companies"
          className="flex-1 min-w-[12rem] rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-3 py-2 text-sm text-zinc-900 dark:text-zinc-100 placeholder:text-zinc-400 focus:outline-none focus:ring-2 focus:ring-zinc-400 dark:focus:ring-zinc-600"
        />

        <select
          name="industry"
          defaultValue={industry}
          aria-label="Filter by industry"
          className={selectClass}
        >
          <option value="">All industries</option>
          {industries.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>

        <select
          name="source"
          defaultValue={source}
          aria-label="Filter by discovery source"
          className={selectClass}
        >
          <option value="">All sources</option>
          {SOURCE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>

        <select
          name="sort"
          defaultValue={sort}
          aria-label="Sort"
          className={selectClass}
        >
          {SORT_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>

        <button
          type="submit"
          className="rounded-md bg-zinc-900 dark:bg-zinc-100 px-4 py-2 text-sm font-medium text-white dark:text-zinc-900 hover:bg-zinc-700 dark:hover:bg-zinc-300 transition-colors"
        >
          Apply
        </button>
        {hasFilters && (
          <Link
            href="/"
            className="text-sm text-zinc-500 dark:text-zinc-400 hover:underline underline-offset-2"
          >
            Clear
          </Link>
        )}
      </form>

      {/* ── Result count ──────────────────────────────────────────────────── */}
      <p className="mb-4 text-sm text-zinc-500 dark:text-zinc-400">
        {total === 0
          ? "No matching companies."
          : `Showing ${firstShown}–${lastShown} of ${total.toLocaleString("en-US")} ${total === 1 ? "company" : "companies"}`}
      </p>

      {/* ── Company grid ──────────────────────────────────────────────────── */}
      {companies.length === 0 ? (
        <div className="rounded-lg border border-dashed border-zinc-300 dark:border-zinc-700 px-8 py-14 text-center">
          <p className="text-zinc-500 dark:text-zinc-400">
            {hasFilters
              ? "No companies match these filters."
              : "No companies indexed yet. Run the discovery pipeline:"}
          </p>
          {!hasFilters && (
            <pre className="mt-4 inline-block rounded bg-zinc-100 dark:bg-zinc-800 px-4 py-2 text-sm text-zinc-700 dark:text-zinc-300 font-mono">
              <code>nous refresh-vc-portfolios</code>
            </pre>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {companies.map((company) => (
            <Link
              key={company.slug}
              href={`/c/${company.slug}`}
              className="group block rounded-lg border border-zinc-200 dark:border-zinc-800 p-5 hover:border-zinc-400 dark:hover:border-zinc-600 transition-colors"
            >
              <h2 className="font-semibold text-zinc-900 dark:text-zinc-100 group-hover:underline underline-offset-2 leading-snug">
                {company.name}
              </h2>

              {company.description_short && (
                <p className="mt-2 text-sm text-zinc-500 dark:text-zinc-400 line-clamp-2 leading-snug">
                  {company.description_short}
                </p>
              )}

              <dl className="mt-3 space-y-1 text-sm text-zinc-500 dark:text-zinc-400">
                {(company.hq_city || company.hq_state) && (
                  <div className="flex justify-between gap-2">
                    <dt className="sr-only">Location</dt>
                    <dd>{formatLocation(company.hq_city, company.hq_state)}</dd>
                  </div>
                )}
                {company.industry_group && (
                  <div>
                    <dt className="sr-only">Industry</dt>
                    <dd className="truncate">{company.industry_group}</dd>
                  </div>
                )}
              </dl>
            </Link>
          ))}
        </div>
      )}

      {/* ── Pagination ────────────────────────────────────────────────────── */}
      {totalPages > 1 && (
        <nav
          className="mt-10 flex items-center justify-between"
          aria-label="Pagination"
        >
          {page > 1 ? (
            <Link
              href={hrefForPage(page - 1)}
              rel="prev"
              className="rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm text-zinc-700 dark:text-zinc-300 hover:border-zinc-400 dark:hover:border-zinc-600 transition-colors"
            >
              ← Previous
            </Link>
          ) : (
            <span className="rounded-md border border-zinc-200 dark:border-zinc-800 px-4 py-2 text-sm text-zinc-300 dark:text-zinc-700 cursor-default">
              ← Previous
            </span>
          )}

          <span className="text-sm text-zinc-500 dark:text-zinc-400">
            Page {page} of {totalPages}
          </span>

          {page < totalPages ? (
            <Link
              href={hrefForPage(page + 1)}
              rel="next"
              className="rounded-md border border-zinc-300 dark:border-zinc-700 px-4 py-2 text-sm text-zinc-700 dark:text-zinc-300 hover:border-zinc-400 dark:hover:border-zinc-600 transition-colors"
            >
              Next →
            </Link>
          ) : (
            <span className="rounded-md border border-zinc-200 dark:border-zinc-800 px-4 py-2 text-sm text-zinc-300 dark:text-zinc-700 cursor-default">
              Next →
            </span>
          )}
        </nav>
      )}
    </main>
  );
}
