"use client";

// Accountless watchlist toggle (Task C3). Persists a set of company slugs in
// localStorage under `nous:watchlist`; the /watchlist page reads the same key.
// No server round-trip, no auth — the whole feature lives in the browser.
//
// Reads use useSyncExternalStore (not an effect + setState) so the component
// stays in sync with the external localStorage "store" without cascading
// renders, and SSR/first-paint use a stable server snapshot to avoid hydration
// mismatches.

import { useCallback, useSyncExternalStore } from "react";

export const WATCHLIST_KEY = "nous:watchlist";
const CHANGE_EVENT = "nous:watchlist-change";

/** Read the watchlist slug array from localStorage, tolerating bad/old data. */
export function readWatchlist(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(WATCHLIST_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((s): s is string => typeof s === "string");
  } catch {
    return [];
  }
}

function writeWatchlist(slugs: string[]): void {
  try {
    window.localStorage.setItem(WATCHLIST_KEY, JSON.stringify(slugs));
    // Notify same-tab subscribers (the storage event only fires cross-tab).
    window.dispatchEvent(new Event(CHANGE_EVENT));
  } catch {
    // Quota/private-mode failures are non-fatal — the toggle just won't persist.
  }
}

/** Subscribe to watchlist changes (same-tab custom event + cross-tab storage). */
function subscribe(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/**
 * Whether `slug` is currently on the watchlist, as an external-store hook.
 * Server snapshot is always `false` so SSR and the first client render agree;
 * the real value resolves after hydration without an effect.
 */
export function useIsWatched(slug: string): boolean {
  const getSnapshot = useCallback(
    () => readWatchlist().includes(slug),
    [slug],
  );
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}

/** Toggle a slug on/off the watchlist. */
export function toggleWatch(slug: string): void {
  const current = readWatchlist();
  const next = current.includes(slug)
    ? current.filter((s) => s !== slug)
    : [...current, slug];
  writeWatchlist(next);
}

interface WatchlistButtonProps {
  slug: string;
  /** Accessible label context, e.g. the company name. */
  name: string;
  /** Visual style: a small icon-only star (card overlay) or a labeled button. */
  variant?: "icon" | "labeled";
}

/** A star toggle that adds/removes `slug` from the localStorage watchlist. */
export function WatchlistButton({
  slug,
  name,
  variant = "icon",
}: WatchlistButtonProps) {
  const saved = useIsWatched(slug);

  const onClick = useCallback(() => toggleWatch(slug), [slug]);

  const label = saved
    ? `Remove ${name} from watchlist`
    : `Add ${name} to watchlist`;

  if (variant === "labeled") {
    return (
      <button
        type="button"
        onClick={onClick}
        aria-pressed={saved}
        aria-label={label}
        title={label}
        className="inline-flex items-center gap-1.5 rounded-md border border-edge px-3 py-1.5 text-sm text-ink-soft hover:border-ink-muted hover:text-ink transition-colors"
      >
        <span aria-hidden className={saved ? "text-accent" : ""}>
          {saved ? "★" : "☆"}
        </span>
        {saved ? "Watching" : "Watch"}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={saved}
      aria-label={label}
      title={label}
      className="rounded-md p-1 text-lg leading-none text-ink-muted hover:text-accent transition-colors focus:outline-none focus:ring-2 focus:ring-accent/40"
    >
      <span aria-hidden className={saved ? "text-accent" : ""}>
        {saved ? "★" : "☆"}
      </span>
    </button>
  );
}
