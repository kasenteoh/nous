// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import {
  listCompanies,
  listCompaniesHybrid,
  listIndustryGroups,
  listDiscoveredViaValues,
  searchHuskFallback,
  type CompanyListOptions,
  type CompanyListSort,
} from "@/lib/queries";
import { embedQuery } from "@/lib/embed-query";
import { discoveredViaLabel } from "@/lib/format";
import { CompanyCard } from "@/components/CompanyCard";
import { SaveSearch } from "@/components/SaveSearch";
import { FilterPanel, type ActiveFilterChip } from "@/components/FilterPanel";

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
  { value: "funding_desc", label: "Largest raise" },
  { value: "recently_funded", label: "Recently funded" },
  { value: "headcount_desc", label: "Headcount (high→low)" },
];

// Funding-stage options for the filter dropdown. Values match the free-text
// latest_round_type written by extract-funding; the canonical ladder covers the
// common cases. Odd/unknown stages still surface via the other sorts/filters —
// this is a convenience scope, not an enum.
const STAGE_OPTIONS = [
  "Pre-Seed",
  "Seed",
  "Series A",
  "Series B",
  "Series C",
  "Series D",
  "Series E",
] as const;

// "Funded since" presets (days). 0 = any time (no filter).
const FUNDED_SINCE_OPTIONS: { value: number; label: string }[] = [
  { value: 0, label: "Any time" },
  { value: 90, label: "Last 90 days" },
  { value: 180, label: "Last 180 days" },
  { value: 365, label: "Last year" },
  { value: 730, label: "Last 2 years" },
];


type SearchParams = {
  q?: string | string[];
  industry?: string | string[];
  source?: string | string[];
  sort?: string | string[];
  page?: string | string[];
  // Advanced VC filters (Task C2).
  min_raised?: string | string[];
  max_raised?: string | string[];
  founded_after?: string | string[];
  founded_before?: string | string[];
  emp_min?: string | string[];
  emp_max?: string | string[];
  stage?: string | string[];
  funded_since_days?: string | string[];
};

function firstStr(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

/**
 * Parse a search-param value as a non-negative number. Returns undefined for
 * empty/NaN/negative input so the corresponding filter is simply omitted.
 */
function firstNum(value: string | string[] | undefined): number | undefined {
  const raw = firstStr(value).trim();
  if (!raw) return undefined;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : undefined;
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
  // An explicit, valid ?sort= param. Distinct from the *effective* sort below:
  // semantic blending only happens under the default ordering — appending
  // cosine-ranked extras to an explicitly requested "Name (A–Z)" or "Largest
  // raise" list would misorder it (see listCompaniesHybrid).
  const explicitSort = SORT_OPTIONS.some((o) => o.value === sortRaw);
  const sort: CompanyListSort = explicitSort
    ? (sortRaw as CompanyListSort)
    : "name_asc";

  // Advanced filters (Task C2). Numbers parsed defensively; stage validated
  // against the known ladder so an arbitrary ?stage= can't inject odd values.
  const minRaised = firstNum(sp.min_raised);
  const maxRaised = firstNum(sp.max_raised);
  const foundedAfter = firstNum(sp.founded_after);
  const foundedBefore = firstNum(sp.founded_before);
  const empMin = firstNum(sp.emp_min);
  const empMax = firstNum(sp.emp_max);
  const stageRaw = firstStr(sp.stage);
  const stage = (STAGE_OPTIONS as readonly string[]).includes(stageRaw)
    ? stageRaw
    : "";
  const fundedSinceRaw = firstNum(sp.funded_since_days) ?? 0;
  const fundedSinceDays = FUNDED_SINCE_OPTIONS.some(
    (o) => o.value === fundedSinceRaw,
  )
    ? fundedSinceRaw
    : 0;

  // The column-scoped filters, built once and reused for both the listing fetch
  // and any clamped re-fetch.
  const filters: CompanyListOptions = {
    search: q || undefined,
    industry_group: industry || undefined,
    discovered_via: source || undefined,
    min_raised: minRaised,
    max_raised: maxRaised,
    founded_after: foundedAfter,
    founded_before: foundedBefore,
    emp_min: empMin,
    emp_max: empMax,
    stage: stage || undefined,
    funded_since_days: fundedSinceDays || undefined,
  };

  // Parse requested page (NaN / negative → 1).
  const parsedPage = Math.max(1, Number.parseInt(firstStr(sp.page), 10) || 1);

  // ── First pass: fetch with requested page to learn the real total ──────────
  // If the page is out of range, listCompanies returns rows=[] and the real
  // total via a count fallback. We then clamp and optionally re-fetch.
  const offset = (parsedPage - 1) * PAGE_SIZE;

  // Semantic search (E-2): embed the query server-side only when the result
  // could actually blend — page 1, default sort, non-empty q, and no column
  // filters (the semantic RPC ranks the whole catalog, so extras under an
  // active filter could violate it — see listCompaniesHybrid, which gates on
  // the same conditions; this check just avoids pointless model work).
  // embedQuery returns null on ANY failure (no model, timeout, secret-free
  // CI), which listCompaniesHybrid treats as "pure lexical" — search never
  // breaks on the model. `sort: undefined` (vs the effective "name_asc") is
  // the blend gate's signal that the ordering is the default, not user-chosen.
  const columnFiltersActive =
    Boolean(industry || source || stage || fundedSinceDays) ||
    minRaised != null ||
    maxRaised != null ||
    foundedAfter != null ||
    foundedBefore != null ||
    empMin != null ||
    empMax != null;
  const semanticEligible =
    Boolean(q) && parsedPage === 1 && !explicitSort && !columnFiltersActive;
  const queryEmbedding = semanticEligible ? await embedQuery(q) : null;

  const [firstResult, industries, discoveredViaValues] = await Promise.all([
    listCompaniesHybrid(
      {
        ...filters,
        sort: explicitSort ? sort : undefined,
        limit: PAGE_SIZE,
        offset,
      },
      queryEmbedding,
    ),
    listIndustryGroups(),
    listDiscoveredViaValues(),
  ]);

  const { total, semanticCount } = firstResult;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  // Clamp the effective page to [1, totalPages].
  const page = Math.min(totalPages, Math.max(1, parsedPage));

  // Re-fetch only when the page was clamped (i.e. the user requested an
  // out-of-range page and rows came back empty because the offset overshot).
  let companies = firstResult.rows;
  if (page !== parsedPage && companies.length === 0 && total > 0) {
    const clampedOffset = (page - 1) * PAGE_SIZE;
    const refetch = await listCompanies({
      ...filters,
      sort,
      limit: PAGE_SIZE,
      offset: clampedOffset,
    });
    companies = refetch.rows;
  }

  const hasFilters = Boolean(
    q ||
      industry ||
      source ||
      minRaised != null ||
      maxRaised != null ||
      foundedAfter != null ||
      foundedBefore != null ||
      empMin != null ||
      empMax != null ||
      stage ||
      fundedSinceDays,
  );
  const effectiveOffset = (page - 1) * PAGE_SIZE;
  const firstShown = total === 0 ? 0 : effectiveOffset + 1;
  const lastShown = Math.min(effectiveOffset + companies.length, total);

  // Husk fallback: when the main search returns 0 LEXICAL results for a
  // non-empty query, look for name-matching husks (companies with no
  // description that passed exclusion but failed the catalog bar). Keyed on
  // the lexical total, not the blended rows: semantic neighbors landing for
  // "Anthropic" must not suppress the "we track Anthropic but have no full
  // profile yet" box — that box is about name matches.
  const huskFallback =
    firstResult.lexicalTotal === 0 && q ? await searchHuskFallback(q) : [];

  // The active filter/sort set as URLSearchParams (WITHOUT page). Single source
  // of truth reused by pagination links, the Save-Search button, and the CSV
  // export link so all three carry the exact same shortlist.
  const baseParams = (): URLSearchParams => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (industry) params.set("industry", industry);
    if (source) params.set("source", source);
    if (sort !== "name_asc") params.set("sort", sort);
    if (minRaised != null) params.set("min_raised", String(minRaised));
    if (maxRaised != null) params.set("max_raised", String(maxRaised));
    if (foundedAfter != null) params.set("founded_after", String(foundedAfter));
    if (foundedBefore != null) {
      params.set("founded_before", String(foundedBefore));
    }
    if (empMin != null) params.set("emp_min", String(empMin));
    if (empMax != null) params.set("emp_max", String(empMax));
    if (stage) params.set("stage", stage);
    if (fundedSinceDays) {
      params.set("funded_since_days", String(fundedSinceDays));
    }
    return params;
  };

  // Build a /companies?… href for a target page, preserving filters/sort.
  const hrefForPage = (target: number): string => {
    const params = baseParams();
    if (target > 1) params.set("page", String(target));
    const qs = params.toString();
    return qs ? `/companies?${qs}` : "/companies";
  };

  // Querystring (no leading "?") that the current filter set maps to — for the
  // Save-Search localStorage entry and the CSV export route.
  const currentQuery = baseParams().toString();
  const exportHref = currentQuery
    ? `/api/export?${currentQuery}`
    : "/api/export";

  // ── Advanced-filter chips (for the collapsible Filters panel) ──────────────
  // The advanced filters are tucked into <FilterPanel>. When that panel is
  // collapsed it shows a removable chip per active advanced filter; each chip
  // links back to /companies with just that one param dropped (every other
  // active filter/sort preserved). These hrefs are computed here, server-side,
  // because they depend on the full current param set.

  // Compact USD label for raised-amount chips: $1M, $2.5M, $750K, else the raw
  // number. Keeps chips short without a heavy i18n dependency.
  const usdShort = (n: number): string => {
    if (n >= 1_000_000) {
      const m = n / 1_000_000;
      return `$${Number.isInteger(m) ? m : m.toFixed(1)}M`;
    }
    if (n >= 1_000) {
      const k = n / 1_000;
      return `$${Number.isInteger(k) ? k : k.toFixed(1)}K`;
    }
    return `$${n.toLocaleString("en-US")}`;
  };

  // /companies?… href with one advanced param removed, preserving everything
  // else. Built from baseParams() (which already excludes `page`).
  const hrefWithout = (key: string): string => {
    const params = baseParams();
    params.delete(key);
    const qs = params.toString();
    return qs ? `/companies?${qs}` : "/companies";
  };

  const advancedChips: ActiveFilterChip[] = [];
  if (source) {
    advancedChips.push({
      key: "source",
      label: `Source: ${discoveredViaLabel(source)}`,
      removeHref: hrefWithout("source"),
    });
  }
  if (stage) {
    advancedChips.push({
      key: "stage",
      label: `Stage: ${stage}`,
      removeHref: hrefWithout("stage"),
    });
  }
  if (fundedSinceDays) {
    const opt = FUNDED_SINCE_OPTIONS.find((o) => o.value === fundedSinceDays);
    advancedChips.push({
      key: "funded_since_days",
      label: `Funded: ${opt?.label ?? `${fundedSinceDays}d`}`,
      removeHref: hrefWithout("funded_since_days"),
    });
  }
  if (minRaised != null) {
    advancedChips.push({
      key: "min_raised",
      label: `Raised ≥ ${usdShort(minRaised)}`,
      removeHref: hrefWithout("min_raised"),
    });
  }
  if (maxRaised != null) {
    advancedChips.push({
      key: "max_raised",
      label: `Raised ≤ ${usdShort(maxRaised)}`,
      removeHref: hrefWithout("max_raised"),
    });
  }
  if (foundedAfter != null) {
    advancedChips.push({
      key: "founded_after",
      label: `Founded ≥ ${foundedAfter}`,
      removeHref: hrefWithout("founded_after"),
    });
  }
  if (foundedBefore != null) {
    advancedChips.push({
      key: "founded_before",
      label: `Founded ≤ ${foundedBefore}`,
      removeHref: hrefWithout("founded_before"),
    });
  }
  if (empMin != null) {
    advancedChips.push({
      key: "emp_min",
      label: `Employees ≥ ${empMin}`,
      removeHref: hrefWithout("emp_min"),
    });
  }
  if (empMax != null) {
    advancedChips.push({
      key: "emp_max",
      label: `Employees ≤ ${empMax}`,
      removeHref: hrefWithout("emp_max"),
    });
  }

  // "Clear filters" inside the panel drops every advanced param but keeps the
  // primary controls (search / industry / sort) the user set on the main bar.
  const clearAdvancedParams = baseParams();
  for (const key of [
    "source",
    "stage",
    "funded_since_days",
    "min_raised",
    "max_raised",
    "founded_after",
    "founded_before",
    "emp_min",
    "emp_max",
  ]) {
    clearAdvancedParams.delete(key);
  }
  const clearAdvancedQs = clearAdvancedParams.toString();
  const clearAdvancedHref = clearAdvancedQs
    ? `/companies?${clearAdvancedQs}`
    : "/companies";

  const selectClass =
    "rounded-md border border-edge bg-canvas px-3 py-2 text-sm text-ink-soft focus:outline-none focus:ring-2 focus:ring-accent/40";
  const numInputClass =
    "w-28 rounded-md border border-edge bg-canvas px-3 py-2 text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:ring-2 focus:ring-accent/40";

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
        className="mb-6 flex flex-col gap-3"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
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
        </div>

        {/* Advanced VC filters (Task C2), tucked into a collapsible panel so the
            primary bar (search / industry / sort) stays uncluttered. The panel
            lives inside this same GET form, so its inputs submit and round-trip
            through the URL like everything else; it auto-opens when any advanced
            filter is active and shows removable chips while collapsed. */}
        <FilterPanel
          chips={advancedChips}
          clearAdvancedHref={clearAdvancedHref}
        >
          <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
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
                  {discoveredViaLabel(value)}
                </option>
              ))}
            </select>

            <select
              name="stage"
              defaultValue={stage}
              aria-label="Filter by funding stage"
              className={selectClass}
            >
              <option value="">Any stage</option>
              {STAGE_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>

            <select
              name="funded_since_days"
              defaultValue={String(fundedSinceDays)}
              aria-label="Filter by how recently funded"
              className={selectClass}
            >
              {FUNDED_SINCE_OPTIONS.map((o) => (
                <option key={o.value} value={String(o.value)}>
                  {o.label}
                </option>
              ))}
            </select>

            <div className="flex items-center gap-2 text-sm text-ink-muted">
              <span className="sr-only sm:not-sr-only">Raised</span>
              <input
                type="number"
                name="min_raised"
                min={0}
                step={1000000}
                defaultValue={minRaised ?? ""}
                placeholder="min $"
                aria-label="Minimum total raised (USD)"
                className={numInputClass}
              />
              <span aria-hidden>–</span>
              <input
                type="number"
                name="max_raised"
                min={0}
                step={1000000}
                defaultValue={maxRaised ?? ""}
                placeholder="max $"
                aria-label="Maximum total raised (USD)"
                className={numInputClass}
              />
            </div>

            <div className="flex items-center gap-2 text-sm text-ink-muted">
              <span className="sr-only sm:not-sr-only">Founded</span>
              <input
                type="number"
                name="founded_after"
                min={1900}
                max={2100}
                step={1}
                defaultValue={foundedAfter ?? ""}
                placeholder="after"
                aria-label="Founded in or after year"
                className={numInputClass}
              />
              <span aria-hidden>–</span>
              <input
                type="number"
                name="founded_before"
                min={1900}
                max={2100}
                step={1}
                defaultValue={foundedBefore ?? ""}
                placeholder="before"
                aria-label="Founded in or before year"
                className={numInputClass}
              />
            </div>

            <div className="flex items-center gap-2 text-sm text-ink-muted">
              <span className="sr-only sm:not-sr-only">Employees</span>
              <input
                type="number"
                name="emp_min"
                min={0}
                step={1}
                defaultValue={empMin ?? ""}
                placeholder="min"
                aria-label="Minimum employees"
                className={numInputClass}
              />
              <span aria-hidden>–</span>
              <input
                type="number"
                name="emp_max"
                min={0}
                step={1}
                defaultValue={empMax ?? ""}
                placeholder="max"
                aria-label="Maximum employees"
                className={numInputClass}
              />
            </div>
          </div>
        </FilterPanel>

        <div className="flex flex-wrap items-center gap-3">
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

          {/* Save the current filter set to localStorage (Task C3). */}
          <SaveSearch query={currentQuery} />

          {/* Stream the full current shortlist as CSV (Task C4). */}
          <a
            href={exportHref}
            className="text-sm text-ink-soft hover:text-ink underline underline-offset-2 decoration-ink-faint"
          >
            Export CSV
          </a>

          <Link
            href="/watchlist"
            className="ml-auto text-sm text-ink-soft hover:text-ink underline underline-offset-2 decoration-ink-faint"
          >
            My watchlist →
          </Link>
        </div>
      </form>

      {/* ── Result count ──────────────────────────────────────────────────── */}
      <p className="mb-4 text-sm text-ink-muted">
        {total === 0
          ? "No matching companies."
          : `Showing ${firstShown}–${lastShown} of ${total.toLocaleString("en-US")} ${total === 1 ? "company" : "companies"}`}
        {/* Disclose when semantically related (not literally matching)
            companies were appended to the results (E-2). */}
        {semanticCount > 0 && (
          <span className="text-ink-faint"> · includes semantic matches</span>
        )}
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
            <CompanyCard
              key={company.slug}
              company={company}
              logoUrl={company.logo_url}
            />
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
