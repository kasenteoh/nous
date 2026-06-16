"use client";

// The two client surfaces of the browser-local compare set (lib/compare.ts):
//
//   • CompareToggle — a per-card checkbox island used by CompanyCard, sitting
//     next to the watchlist star. It writes the set.
//   • CompareBar — a sticky bottom bar (rendered globally from app/layout.tsx)
//     that reads the set and links to /compare. /compare otherwise only works
//     via a hand-typed ?slugs=a,b querystring — this is the missing UI.
//
// They share one localStorage-backed external store read through
// useSyncExternalStore, so they stay in sync with same-tab and cross-tab edits
// and never cause a hydration mismatch (the server / first-paint snapshot is
// the empty set, so the toggle renders unchecked and the bar renders nothing
// until hydration resolves the real selection).

import Link from "next/link";
import { useCallback } from "react";
import {
  clearCompare,
  MAX_COMPARE,
  toggleCompare,
  useCompareSet,
  useIsComparing,
} from "@/lib/compare";

// /compare wants at least 2 companies to render a meaningful table.
const MIN_COMPARE = 2;

interface CompareToggleProps {
  slug: string;
  /** Accessible label context, e.g. the company name. */
  name: string;
}

/**
 * A small "Compare" checkbox that adds/removes `slug` from the compare set.
 * Rendered by CompanyCard alongside the watchlist star. A real <input
 * type="checkbox"> for free keyboard + screen-reader semantics; the visible
 * "Compare" word is the <label>, so the whole control is one click/tap target.
 *
 * Disabled (but kept visible) when the set is full and this card isn't in it,
 * so the 4-company cap is legible rather than a silent no-op.
 */
export function CompareToggle({ slug, name }: CompareToggleProps) {
  const selected = useIsComparing(slug);
  const set = useCompareSet();
  const full = set.length >= MAX_COMPARE;
  const disabled = full && !selected;

  const onChange = useCallback(() => {
    toggleCompare(slug);
  }, [slug]);

  const label = selected
    ? `Remove ${name} from compare`
    : disabled
      ? `Compare is full (max ${MAX_COMPARE}); remove one to add ${name}`
      : `Add ${name} to compare`;

  return (
    <label
      title={label}
      className={`inline-flex items-center gap-1 text-xs select-none ${
        disabled
          ? "text-ink-faint cursor-not-allowed"
          : "text-ink-muted hover:text-ink cursor-pointer"
      }`}
    >
      <input
        type="checkbox"
        checked={selected}
        disabled={disabled}
        onChange={onChange}
        aria-label={label}
        className="h-3.5 w-3.5 rounded-sm border-edge bg-transparent text-accent accent-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed"
      />
      <span aria-hidden>Compare</span>
    </label>
  );
}

export function CompareBar() {
  const slugs = useCompareSet();
  const count = slugs.length;

  // Hidden until the visitor has picked at least one company. Because the
  // server/first-paint snapshot is empty, this also means the bar is absent
  // during SSR and hydration — no layout shift, no mismatch.
  if (count === 0) return null;

  const ready = count >= MIN_COMPARE && count <= MAX_COMPARE;
  const compareHref = `/compare?slugs=${encodeURIComponent(slugs.join(","))}`;
  // Below the minimum, explain what's missing; at/above it the action is live.
  const hint = ready ? null : `Pick ${MIN_COMPARE - count} more to compare`;

  return (
    <div
      role="region"
      aria-label="Compare selection"
      className="sticky bottom-0 z-30 border-t border-edge bg-canvas/95 backdrop-blur-sm"
    >
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex items-baseline gap-2 text-sm">
          {/* aria-live so screen-reader users hear the count change as they
              tick companies, without moving focus. */}
          <span aria-live="polite" className="text-ink">
            <span className="font-mono font-semibold text-accent">{count}</span>{" "}
            selected
            <span className="text-ink-muted"> · max {MAX_COMPARE}</span>
          </span>
          {hint && (
            <span className="text-xs text-ink-muted">{hint}</span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={clearCompare}
            className="rounded-md border border-edge px-3 py-1.5 text-sm text-ink-soft hover:border-ink-muted hover:text-ink transition-colors"
          >
            Clear
          </button>

          {ready ? (
            <Link
              href={compareHref}
              className="rounded-md border border-accent bg-accent/10 px-4 py-1.5 text-sm font-medium text-accent hover:bg-accent/20 transition-colors"
            >
              Compare {count} →
            </Link>
          ) : (
            // Below the minimum the action isn't navigable yet — render a
            // disabled button (not a dead link) so keyboard/SR users get the
            // disabled semantics and the reason via the title.
            <button
              type="button"
              disabled
              title={hint ?? undefined}
              className="rounded-md border border-edge px-4 py-1.5 text-sm font-medium text-ink-muted opacity-60 cursor-not-allowed"
            >
              Compare →
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
