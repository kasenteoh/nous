"use client";

// Save-search button (Task C3). Stores the current /companies querystring in
// localStorage under `nous:searches` so a VC can re-open a filter set later
// (the /watchlist page lists saved searches as links). Accountless — no server.
//
// "Already saved?" is read via useSyncExternalStore (external localStorage
// store), avoiding an effect+setState. The transient "Saved" flash is plain
// local state set from the click handler (not an effect), which the linter
// allows.

import { useCallback, useState, useSyncExternalStore } from "react";

export const SEARCHES_KEY = "nous:searches";
const CHANGE_EVENT = "nous:searches-change";

/** One saved search: the raw querystring (no leading "?") + when it was saved. */
export interface SavedSearch {
  /** Querystring without the leading "?". Empty string = the unfiltered list. */
  query: string;
  /** ms since epoch, for ordering newest-first. */
  savedAt: number;
}

/** Read saved searches from localStorage, tolerating bad/old data. */
export function readSavedSearches(): SavedSearch[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(SEARCHES_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (s): s is SavedSearch =>
        typeof s === "object" &&
        s !== null &&
        typeof (s as SavedSearch).query === "string" &&
        typeof (s as SavedSearch).savedAt === "number",
    );
  } catch {
    return [];
  }
}

export function writeSavedSearches(searches: SavedSearch[]): void {
  try {
    window.localStorage.setItem(SEARCHES_KEY, JSON.stringify(searches));
    window.dispatchEvent(new Event(CHANGE_EVENT));
  } catch {
    // Quota/private-mode failures are non-fatal.
  }
}

/** Subscribe to saved-search changes (same-tab custom event + cross-tab storage). */
export function subscribeSavedSearches(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

// Cached snapshot for useSyncExternalStore: that hook compares snapshots with
// Object.is and loops forever if getSnapshot returns a fresh array each call.
// We cache the parsed array keyed on the raw localStorage string, so the same
// reference is returned until the stored value actually changes.
let _cachedRaw: string | null = null;
let _cachedSearches: SavedSearch[] = [];

/**
 * Referentially-stable snapshot of saved searches, for useSyncExternalStore.
 * Returns the same array instance until the underlying localStorage value
 * changes.
 */
export function getSavedSearchesSnapshot(): SavedSearch[] {
  if (typeof window === "undefined") return _cachedSearches;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(SEARCHES_KEY);
  } catch {
    raw = null;
  }
  if (raw === _cachedRaw) return _cachedSearches;
  _cachedRaw = raw;
  _cachedSearches = readSavedSearches();
  return _cachedSearches;
}

interface SaveSearchProps {
  /** The current filter set as a querystring (no leading "?"). */
  query: string;
}

/**
 * A button that saves the current filter querystring to localStorage. Reflects
 * whether this exact query is already saved (external store) and shows a brief
 * "Saved" confirmation after a save.
 */
export function SaveSearch({ query }: SaveSearchProps) {
  const [justSaved, setJustSaved] = useState(false);

  const alreadySaved = useSyncExternalStore(
    subscribeSavedSearches,
    useCallback(
      () => readSavedSearches().some((s) => s.query === query),
      [query],
    ),
    () => false,
  );

  const save = useCallback(() => {
    const current = readSavedSearches();
    if (current.some((s) => s.query === query)) return;
    // Newest-first; cap at 30 to keep localStorage bounded.
    const next = [{ query, savedAt: Date.now() }, ...current].slice(0, 30);
    writeSavedSearches(next);
    setJustSaved(true);
    window.setTimeout(() => setJustSaved(false), 1500);
  }, [query]);

  const label = justSaved
    ? "Saved"
    : alreadySaved
      ? "Search saved"
      : "Save search";

  return (
    <button
      type="button"
      onClick={save}
      disabled={alreadySaved && !justSaved}
      aria-label={label}
      title="Save this filter set to revisit later (stored in your browser)"
      className="inline-flex items-center gap-1.5 rounded-md border border-edge px-3 py-1.5 text-sm text-ink-soft hover:border-ink-muted hover:text-ink transition-colors disabled:opacity-60 disabled:hover:border-edge disabled:hover:text-ink-soft"
    >
      <span aria-hidden>{justSaved ? "✓" : "☆"}</span>
      {label}
    </button>
  );
}
