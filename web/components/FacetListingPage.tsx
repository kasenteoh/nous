// Shared shell for the /tag/[tag] and /location/[state] listing pages.
// Server component: owns the listCompanies fetch, the not-found branch, the
// sort form, the result-count line, the CompanyCard grid, and the pagination
// nav. Each route file reduces to param decoding + metadata + one render of
// this. /companies deliberately does NOT use it — that page diverges (search
// box, filter dropdowns, zero-result empty state instead of a 404).

import Link from "next/link";
import { notFound } from "next/navigation";
import { listCompanies, type CompanyListSort } from "@/lib/queries";
import { CompanyCard } from "@/components/CompanyCard";

const PAGE_SIZE = 30;

const SORT_OPTIONS: { value: CompanyListSort; label: string }[] = [
  { value: "name_asc", label: "Name (A–Z)" },
  { value: "name_desc", label: "Name (Z–A)" },
  { value: "recent", label: "Recently added" },
];

/** Raw searchParams shape the facet routes receive (after awaiting the Promise). */
export interface FacetSearchParams {
  page?: string | string[];
  sort?: string | string[];
}

function firstStr(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

/**
 * Validate `?page=` and `?sort=`: unrecognized sorts fall back to name_asc,
 * page clamps to ≥ 1. Out-of-range (too-high) pages are not clamped here —
 * the shell 404s them once the fetch comes back empty.
 */
export function parseFacetSearchParams(sp: FacetSearchParams): {
  page: number;
  sort: CompanyListSort;
} {
  const sortRaw = firstStr(sp.sort);
  const sort: CompanyListSort = SORT_OPTIONS.some((o) => o.value === sortRaw)
    ? (sortRaw as CompanyListSort)
    : "name_asc";
  const page = Math.max(1, Number.parseInt(firstStr(sp.page), 10) || 1);
  return { page, sort };
}

interface FacetListingPageProps {
  /** H1 text, e.g. `Tagged “devtools”` or `Startups in California`. */
  heading: string;
  /** Already-encoded path the sort form posts to and pagination links build on, e.g. `/tag/devtools`. */
  basePath: string;
  /** The facet filter forwarded to listCompanies. */
  filter: { tag: string } | { state: string };
  /** 1-based page number, pre-clamped to ≥ 1 by parseFacetSearchParams. */
  page: number;
  sort: CompanyListSort;
}

export async function FacetListingPage({
  heading,
  basePath,
  filter,
  page,
  sort,
}: FacetListingPageProps) {
  const offset = (page - 1) * PAGE_SIZE;

  const { rows: companies, total } = await listCompanies({
    ...filter,
    sort,
    limit: PAGE_SIZE,
    offset,
  });

  // Zero rows on the requested page → clean 404. One branch covers both
  // garbage facet values (total === 0 — no thin pages in the index) and an
  // out-of-range ?page= (total > 0 but the offset is past the end; checking
  // only total would render a "Showing 2971–2 of 2" artifact instead).
  if (companies.length === 0) {
    notFound();
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const firstShown = offset + 1;
  const lastShown = Math.min(offset + companies.length, total);

  // Build a `${basePath}?…` href for a target page, preserving sort.
  const hrefForPage = (target: number): string => {
    const search = new URLSearchParams();
    if (sort !== "name_asc") search.set("sort", sort);
    if (target > 1) search.set("page", String(target));
    const qs = search.toString();
    return qs ? `${basePath}?${qs}` : basePath;
  };

  return (
    <main className="flex-1 px-6 py-12 max-w-6xl mx-auto w-full">
      {/* ── Header ────────────────────────────────────────────────────────── */}
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          {heading}
        </h1>
        <p className="mt-2 text-sm text-ink-muted">
          {total.toLocaleString("en-US")}{" "}
          {total === 1 ? "company" : "companies"}
        </p>
      </header>

      {/* ── Sort ──────────────────────────────────────────────────────────── */}
      <form method="GET" action={basePath} className="mb-6 flex items-center gap-3">
        <label htmlFor="facet-sort" className="text-sm text-ink-muted">
          Sort:
        </label>
        <select
          id="facet-sort"
          name="sort"
          defaultValue={sort}
          className="rounded-md border border-edge bg-canvas px-3 py-2 text-sm text-ink-soft focus:outline-none focus:ring-2 focus:ring-accent/40"
        >
          {SORT_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <button
          type="submit"
          className="rounded-md bg-ink px-4 py-2 text-sm font-medium text-canvas hover:bg-ink/85 transition-colors"
        >
          Apply
        </button>
      </form>

      {/* ── Result count ──────────────────────────────────────────────────── */}
      <p className="mb-4 text-sm text-ink-muted">
        Showing {firstShown}–{lastShown} of {total.toLocaleString("en-US")}{" "}
        {total === 1 ? "company" : "companies"}
      </p>

      {/* ── Company grid ──────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {companies.map((company) => (
          <CompanyCard key={company.slug} company={company} />
        ))}
      </div>

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
