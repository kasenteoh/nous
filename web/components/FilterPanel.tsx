"use client";

// Collapsible "Filters" disclosure for the /companies advanced VC filters
// (min/max raised, founded range, headcount range, stage, funded-since, source).
//
// The page's primary controls (search, industry, sort) stay on the always-visible
// bar; the busier advanced inputs live in here so the bar isn't cluttered.
//
// Why this is a client component: the only interactivity is open/close. Everything
// else — the inputs, their values, the chip links — is rendered server-side and
// passed in. The advanced <input>/<select> elements are this component's
// `children` and live INSIDE the page's GET <form>, so they submit and round-trip
// through the URL exactly like the rest of the bar even while the panel is
// collapsed (a native <details> keeps its subtree in the DOM when closed).
//
// Accessibility: built on native <details>/<summary>, which is keyboard-operable
// (Enter/Space toggles) and exposes the open state to assistive tech for free. We
// only override `open` to auto-expand when filters are active so applied filters
// are never hidden, and track `onToggle` to swap the chevron/label.

import Link from "next/link";
import { useState, type ReactNode } from "react";

/** One active advanced filter, rendered as a removable chip when collapsed. */
export interface ActiveFilterChip {
  /** Stable key (the param name, e.g. "min_raised"). */
  key: string;
  /** Human-readable summary, e.g. "Raised ≥ $1M" or "Stage: Series A". */
  label: string;
  /** /companies?… href with this one param removed (other filters preserved). */
  removeHref: string;
}

interface FilterPanelProps {
  /** Active advanced-filter chips. Non-empty ⇒ the panel auto-opens. */
  chips: ActiveFilterChip[];
  /** /companies href that drops every advanced filter (keeps q/industry/sort). */
  clearAdvancedHref: string;
  /** The advanced filter inputs — rendered inside the page's shared GET form. */
  children: ReactNode;
}

/**
 * A disclosure panel wrapping the advanced filters. Collapsed by default but
 * auto-opens when any advanced filter is active; when collapsed and active it
 * shows a count plus removable chips so applied filters stay visible and one
 * click can drop any single filter.
 */
export function FilterPanel({
  chips,
  clearAdvancedHref,
  children,
}: FilterPanelProps) {
  const activeCount = chips.length;
  // Auto-open when filters are active so they're never hidden; otherwise start
  // collapsed. After mount the user's manual toggles take over via `onToggle`.
  const [open, setOpen] = useState(activeCount > 0);

  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
      className="rounded-md border border-edge bg-canvas"
    >
      <summary
        className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm text-ink-soft hover:text-ink [&::-webkit-details-marker]:hidden"
        // `list-none` + the webkit rule above hide the default disclosure
        // triangle so we can render our own chevron consistently.
      >
        <span
          aria-hidden
          className={`inline-block transition-transform ${open ? "rotate-90" : ""}`}
        >
          ▸
        </span>
        <span className="font-medium">Filters</span>
        {activeCount > 0 && (
          <span className="rounded-full bg-accent/15 px-2 py-0.5 text-xs font-medium text-accent">
            {activeCount} active
          </span>
        )}
        <span className="ml-auto text-xs text-ink-muted">
          {open ? "Hide" : "Show"}
        </span>
      </summary>

      {/* Collapsed summary: removable chips for each active advanced filter, so
          applied filters remain visible (and one-click-removable) without
          expanding the panel. Hidden once expanded to avoid restating the inputs'
          own state. */}
      {!open && activeCount > 0 && (
        <div className="flex flex-wrap items-center gap-2 px-3 pb-3">
          {chips.map((chip) => (
            <Link
              key={chip.key}
              href={chip.removeHref}
              aria-label={`Remove filter: ${chip.label}`}
              className="inline-flex items-center gap-1 rounded-full border border-edge px-2.5 py-1 text-xs text-ink-soft hover:border-ink-muted hover:text-ink transition-colors"
            >
              {chip.label}
              <span aria-hidden className="text-ink-muted">
                ×
              </span>
            </Link>
          ))}
          <Link
            href={clearAdvancedHref}
            className="text-xs text-ink-muted underline underline-offset-2 hover:text-ink"
          >
            Clear filters
          </Link>
        </div>
      )}

      {/* The advanced inputs themselves. Border separates them from the summary
          row. Always in the DOM so collapsed-but-active filters still submit. */}
      <div className="border-t border-edge px-3 py-3">{children}</div>
    </details>
  );
}
