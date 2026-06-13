// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import {
  listCompanies,
  listIndustryGroups,
  listDiscoveredViaValues,
  searchHuskFallback,
  type CompanyListSort,
} from "@/lib/queries";
import { CompanyCard } from "@/components/CompanyCard";

export const metadata: Metadata = {
  // The layout's title template appends " — nous".
  title: "Browse",
  description:
    "Browse, search, and filter every US software startup indexed by nous.",
  // Paramless self-canonical: filter/search/page URLs (?q=…, ?page=…) all
  // collapse to /companies so crawlers don't index the filter combinatorics.
  alternates: { canonical: "/companies" },
};

const PAGE_SIZE = 30;

const SORT_OPTIONS: { value: CompanyListSort; label: string }[] = [
  { value: "name_asc", label: "Name (A–Z)" },
  { value: "name_desc", label: "Name (Z–A)" },
  { value: "recent", label: "Recently added" },
];

// Human-readable labels for discovered_via values. Unknown keys fall back to
// the raw value (title-cased) so new pipeline values self-heal in the UI.
const SOURCE_LABELS: Record<string, string> = {
  vc_portfolio: "VC portfolio",
  techcrunch: "TechCrunch",
  news: "News",
};

function sourceLabel(value: string): string {
  return SOURCE_LABELS[value] ?? value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

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

export default async function CompaniesPage({
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

  // Parse requested page (NaN / negative → 1).
  const parsedPage = Math.max(1, Number.parseInt(firstStr(sp.page), 10) || 1);

  // ── First pass: fetch with requested page to learn the real total ──────────
  // If the page is out of range, listCompanies returns rows=[] and the real
  // total via a count fallback. We then clamp and optionally re-fetch.
  const offset = (parsedPage - 1) * PAGE_SIZE;

  const [firstResult, industries, discoveredViaValues] = await Promise.all([
    listCompanies({
      search: q || undefined,
      industry_group: industry || undefined,
      discovered_via: source || undefined,
      sort,
      limit: PAGE_SIZE,
      offset,
    }),
    listIndustryGroups(),
    listDiscoveredViaValues(),
  ]);

  const { total } = firstResult;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  // Clamp the effective page to [1, totalPages].
  const page = Math.min(totalPages, Math.max(1, parsedPage));

  // Re-fetch only when the page was clamped (i.e. the user requested an
  // out-of-range page and rows came back empty because the offset overshot).
  let companies = firstResult.rows;
  if (page !== parsedPage && companies.length === 0 && total > 0) {
    const clampedOffset = (page - 1) * PAGE_SIZE;
    const refetch = await listCompanies({
      search: q || undefined,
      industry_group: industry || undefined,
      discovered_via: source || undefined,
      sort,
      limit: PAGE_SIZE,
      offset: clampedOffset,
    });
    companies = refetch.rows;
  }

  const hasFilters = Boolean(q || industry || source);
  const effectiveOffset = (page - 1) * PAGE_SIZE;
  const firstShown = total === 0 ? 0 : effectiveOffset + 1;
  const lastShown = Math.min(effectiveOffset + companies.length, total);

  // Husk fallback: when the main search returns 0 results for a non-empty
  // query, look for name-matching husks (companies with no description that
  // passed exclusion but failed the catalog bar).
  const huskFallback =
    companies.length === 0 && q
      ? await searchHuskFallback(q)
      : [];

  // Build a /companies?… href for a target page, preserving the active
  // filters/sort.
  const hrefForPage = (target: number): string => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (industry) params.set("industry", industry);
    if (source) params.set("source", source);
    if (sort !== "name_asc") params.set("sort", sort);
    if (target > 1) params.set("page", String(target));
    const qs = params.toString();
    return qs ? `/companies?${qs}` : "/companies";
  };

  const selectClass =
    "rounded-md border border-edge bg-canvas px-3 py-2 text-sm text-ink-soft focus:outline-none focus:ring-2 focus:ring-accent/40";

  return (
    <main className="flex-1 px-6 py-12 max-w-6xl mx-auto w-full">
      {/* ── Hero ──────────────────────────────────────────────────────────── */}
      <header className="mb-8">
        <h1 className="text-5xl font-semibold tracking-tight text-ink">
          nous
        </h1>
        <p className="mt-3 text-lg text-ink-muted max-w-xl">
          US software startups, discovered from VC portfolios and funding news.
        </p>
      </header>

      {/* ── Search + filters (GET form — URL-driven, no client JS) ─────────── */}
      <form
        method="GET"
        action="/companies"
        className="mb-6 flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center"
      >
        <input
          type="search"
          name="q"
          defaultValue={q}
          placeholder="Search companies…"
          aria-label="Search companies"
          className="flex-1 min-w-[12rem] rounded-md border border-edge bg-canvas px-3 py-2 text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:ring-2 focus:ring-accent/40"
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

        {/* Source filter built from real discovered_via values in the DB —
            self-heals when new pipeline sources appear. */}
        <select
          name="source"
          defaultValue={source}
          aria-label="Filter by discovery source"
          className={selectClass}
        >
          <option value="">All sources</option>
          {discoveredViaValues.map((value) => (
            <option key={value} value={value}>
              {sourceLabel(value)}
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
          className="rounded-md bg-ink px-4 py-2 text-sm font-medium text-canvas hover:bg-ink/85 transition-colors"
        >
          Apply
        </button>
        {hasFilters && (
          <Link
            href="/companies"
            className="text-sm text-ink-muted hover:text-ink hover:underline underline-offset-2"
          >
            Clear
          </Link>
        )}
      </form>

      {/* ── Result count ──────────────────────────────────────────────────── */}
      <p className="mb-4 text-sm text-ink-muted">
        {total === 0
          ? "No matching companies."
          : `Showing ${firstShown}–${lastShown} of ${total.toLocaleString("en-US")} ${total === 1 ? "company" : "companies"}`}
      </p>

      {/* ── Company grid ──────────────────────────────────────────────────── */}
      {companies.length === 0 ? (
        // Task 1.2: only show the pipeline cold-start box when the catalog is
        // genuinely empty (total===0, no filters, first page). Any other
        // zero-result state (out-of-range page, filtered search, etc.) shows
        // a plain "no match" message.
        total === 0 && !hasFilters && page === 1 ? (
          <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
            <p className="text-ink-muted">
              No companies indexed yet. Run the discovery pipeline:
            </p>
            <pre className="mt-4 inline-block rounded border border-edge px-4 py-2 text-sm text-ink-soft font-mono">
              <code>nous refresh-vc-portfolios</code>
            </pre>
          </div>
        ) : (
          <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
            <p className="text-ink-muted">No companies match these filters.</p>
          </div>
        )
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {companies.map((company) => (
            <CompanyCard key={company.slug} company={company} />
          ))}
        </div>
      )}

      {/* ── Task 1.5: Husk fallback — name-only suggestions shown below the main
          grid when the catalog search returned nothing for a non-empty query.
          Kept out of the ranked grid: these companies have no enriched profile
          yet and should not be presented as full results. ─────────────────── */}
      {huskFallback.length > 0 && (
        <div className="mt-8 rounded-lg border border-edge px-8 py-6">
          <p className="text-sm text-ink-muted mb-3">
            We track these but don&apos;t have a full profile yet:
          </p>
          <ul className="flex flex-wrap gap-2">
            {huskFallback.map((h) => (
              <li key={h.slug}>
                <Link
                  href={`/c/${h.slug}`}
                  className="rounded border border-edge px-3 py-1 text-sm text-ink-soft hover:border-ink-muted hover:text-ink transition-colors"
                >
                  {h.name}
                </Link>
              </li>
            ))}
          </ul>
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
