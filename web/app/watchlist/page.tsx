"use client";

// Watchlist + saved-searches page (Task C3). Everything here is sourced from
// localStorage (`nous:watchlist`, `nous:searches`), so the page is a client
// component; company rows are hydrated from slugs via a server action so the
// service-role key stays server-side. Not in the sitemap (per-user, no SSR
// value).
//
// Both localStorage-backed lists are read via useSyncExternalStore (not
// effect+setState) so they stay in sync with cross-tab/in-tab changes. The only
// effect runs the async slug→cards hydration; its setState calls happen after
// the await (allowed) or in cleanup.

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { CompanyCard } from "@/components/CompanyCard";
import { readWatchlist } from "@/components/WatchlistButton";
import {
  getSavedSearchesSnapshot,
  subscribeSavedSearches,
  writeSavedSearches,
  type SavedSearch,
} from "@/components/SaveSearch";
import { fetchWatchlistCompanies } from "./actions";
import type { CompanyListRow } from "@/lib/types";

const WL_CHANGE_EVENT = "nous:watchlist-change";

/** Subscribe to watchlist changes (same-tab custom event + cross-tab storage). */
function subscribeWatchlist(onChange: () => void): () => void {
  window.addEventListener(WL_CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(WL_CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/** Human-readable summary of a saved search's querystring. */
function describeSearch(query: string): string {
  if (!query) return "All companies";
  const params = new URLSearchParams(query);
  const parts: string[] = [];
  const q = params.get("q");
  if (q) parts.push(`“${q}”`);
  const industry = params.get("industry");
  if (industry) parts.push(industry);
  const stage = params.get("stage");
  if (stage) parts.push(stage);
  if (params.get("funded_since_days")) {
    parts.push(`funded ≤${params.get("funded_since_days")}d`);
  }
  const source = params.get("source");
  if (source) parts.push(source);
  const sort = params.get("sort");
  if (sort) parts.push(`sort: ${sort.replace(/_/g, " ")}`);
  if (params.get("min_raised") || params.get("max_raised")) {
    parts.push("raised range");
  }
  if (params.get("emp_min") || params.get("emp_max")) parts.push("headcount");
  if (params.get("founded_after") || params.get("founded_before")) {
    parts.push("founded range");
  }
  return parts.length > 0 ? parts.join(" · ") : "Custom filter";
}

export default function WatchlistPage() {
  // Watchlist slugs as an external store. Server snapshot is "" (empty) so SSR
  // and first paint agree; the real slugs resolve after hydration.
  const slugsKey = useSyncExternalStore(
    subscribeWatchlist,
    () => readWatchlist().join(","),
    () => "",
  );
  const searches = useSyncExternalStore(
    subscribeSavedSearches,
    getSavedSearchesSnapshot,
    () => EMPTY_SEARCHES,
  );

  // Cards keyed to the slug set they were fetched for. `loadedKey === slugsKey`
  // means the displayed cards match the current watchlist (so we can show a
  // "Loading…" state until the first fetch for this key resolves) — this keeps
  // every setState OUT of the synchronous effect body (all are post-await).
  const [companies, setCompanies] = useState<CompanyListRow[]>([]);
  const [missingCount, setMissingCount] = useState(0);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const loaded = loadedKey === slugsKey;

  // Hydrate cards whenever the slug set changes. fetchWatchlistCompanies returns
  // [] for an empty list, so the empty case flows through the same async path —
  // no synchronous setState in the effect body.
  useEffect(() => {
    let cancelled = false;
    const slugs = slugsKey ? slugsKey.split(",") : [];

    fetchWatchlistCompanies(slugs)
      .then((rows) => {
        if (cancelled) return;
        setCompanies(rows);
        setMissingCount(Math.max(0, slugs.length - rows.length));
        setLoadedKey(slugsKey);
      })
      .catch(() => {
        if (cancelled) return;
        setCompanies([]);
        setMissingCount(0);
        setLoadedKey(slugsKey);
      });

    return () => {
      cancelled = true;
    };
  }, [slugsKey]);

  const removeSearch = useCallback(
    (query: string) => {
      writeSavedSearches(searches.filter((s) => s.query !== query));
    },
    [searches],
  );

  return (
    <main className="flex-1 px-6 py-12 max-w-6xl mx-auto w-full">
      <header className="mb-8">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          My watchlist
        </h1>
        <p className="mt-3 text-ink-muted max-w-xl">
          Companies and searches you&apos;ve saved. Stored only in this browser —
          no account needed.
        </p>
      </header>

      {/* ── Saved searches ──────────────────────────────────────────────────── */}
      <section className="mb-12">
        <h2 className="text-lg font-semibold text-ink mb-4">Saved searches</h2>
        {searches.length === 0 ? (
          <p className="text-sm text-ink-muted">
            No saved searches yet. Use “Save search” on the{" "}
            <Link
              href="/companies"
              className="underline underline-offset-2 hover:text-ink"
            >
              browse page
            </Link>
            .
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {searches.map((s) => (
              <li
                key={s.query || "__all__"}
                className="flex items-center justify-between gap-3 rounded-md border border-edge px-4 py-2"
              >
                <Link
                  href={s.query ? `/companies?${s.query}` : "/companies"}
                  className="text-sm text-ink-soft hover:text-ink underline underline-offset-2 decoration-ink-faint"
                >
                  {describeSearch(s.query)}
                </Link>
                <button
                  type="button"
                  onClick={() => removeSearch(s.query)}
                  aria-label="Remove saved search"
                  className="text-xs text-ink-muted hover:text-ink transition-colors"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* ── Watched companies ───────────────────────────────────────────────── */}
      <section>
        <h2 className="text-lg font-semibold text-ink mb-4">Watched companies</h2>
        {!loaded ? (
          <p className="text-sm text-ink-muted">Loading…</p>
        ) : companies.length === 0 ? (
          <div className="rounded-lg border border-dashed border-edge px-8 py-14 text-center">
            <p className="text-ink-muted">
              No companies in your watchlist yet. Star a company on the{" "}
              <Link
                href="/companies"
                className="underline underline-offset-2 hover:text-ink"
              >
                browse page
              </Link>{" "}
              to add it here.
            </p>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
              {companies.map((company) => (
                <CompanyCard
                  key={company.slug}
                  company={company}
                  logoUrl={company.logo_url}
                />
              ))}
            </div>
            {missingCount > 0 && (
              <p className="mt-4 text-xs text-ink-muted">
                {missingCount}{" "}
                {missingCount === 1 ? "company is" : "companies are"} no longer
                listed and were hidden.
              </p>
            )}
          </>
        )}
      </section>
    </main>
  );
}

// Stable empty-array reference for the saved-searches server snapshot.
// useSyncExternalStore requires getServerSnapshot to return a referentially
// stable value (a fresh [] each call would loop), so share one constant.
const EMPTY_SEARCHES: SavedSearch[] = [];
