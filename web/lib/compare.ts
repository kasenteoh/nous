"use client";

// Browser-local "compare set": the slugs the visitor has ticked to compare
// side by side on /compare. Mirrors the watchlist store in
// components/WatchlistButton.tsx — a localStorage-backed set (here under
// `nous:compare`) read through useSyncExternalStore so components stay in sync
// with same-tab and cross-tab changes without an effect+setState, and SSR /
// first paint use a stable empty snapshot to avoid hydration mismatches.
//
// The only material difference from the watchlist is the CAP: /compare renders
// at most 4 companies (see app/compare/page.tsx MAX_COMPARE), so the set is
// capped at MAX_COMPARE here too. `add`/`toggle` are no-ops once full, so the
// UI can't push the user past a slug count the compare page would silently drop.

import { useCallback, useSyncExternalStore } from "react";

export const COMPARE_KEY = "nous:compare";
const CHANGE_EVENT = "nous:compare-change";

/** Max companies the /compare page renders side by side. Keep in sync with
 *  MAX_COMPARE in app/compare/page.tsx. */
export const MAX_COMPARE = 4;

/** Read the compare slug array from localStorage, tolerating bad/old data.
 *  Deduped and capped at MAX_COMPARE so a tampered payload can't grow the set. */
export function readCompareSet(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(COMPARE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const seen = new Set<string>();
    for (const s of parsed) {
      if (typeof s === "string" && s) seen.add(s);
      if (seen.size >= MAX_COMPARE) break;
    }
    return [...seen];
  } catch {
    return [];
  }
}

function writeCompareSet(slugs: string[]): void {
  try {
    window.localStorage.setItem(COMPARE_KEY, JSON.stringify(slugs));
    // Notify same-tab subscribers (the storage event only fires cross-tab).
    window.dispatchEvent(new Event(CHANGE_EVENT));
  } catch {
    // Quota/private-mode failures are non-fatal — the toggle just won't persist.
  }
}

/** Subscribe to compare-set changes (same-tab custom event + cross-tab storage). */
function subscribe(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

// Cached snapshot for useSyncExternalStore: the hook compares snapshots with
// Object.is and loops forever if getSnapshot returns a fresh array each call.
// Cache the parsed array keyed on the raw localStorage string, so the same
// reference is returned until the stored value actually changes. (Same trick as
// getSavedSearchesSnapshot in components/SaveSearch.tsx.)
let _cachedRaw: string | null = null;
let _cachedSet: string[] = [];

function getCompareSnapshot(): string[] {
  if (typeof window === "undefined") return _cachedSet;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(COMPARE_KEY);
  } catch {
    raw = null;
  }
  if (raw === _cachedRaw) return _cachedSet;
  _cachedRaw = raw;
  _cachedSet = readCompareSet();
  return _cachedSet;
}

// Stable empty-array reference for the server snapshot. useSyncExternalStore
// requires getServerSnapshot to return a referentially stable value (a fresh []
// each call would loop), so SSR and first client paint share this one constant.
const EMPTY: string[] = [];

/**
 * The current compare set as an external-store hook. Returns a referentially
 * stable array (same instance until the stored value changes). Server snapshot
 * is the empty array so SSR and the first client render agree; the real slugs
 * resolve after hydration without an effect.
 */
export function useCompareSet(): string[] {
  return useSyncExternalStore(subscribe, getCompareSnapshot, () => EMPTY);
}

/** Whether `slug` is currently in the compare set, as an external-store hook. */
export function useIsComparing(slug: string): boolean {
  const getSnapshot = useCallback(
    () => readCompareSet().includes(slug),
    [slug],
  );
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}

/** Add `slug` to the compare set. No-op if already present or the set is full. */
export function addToCompare(slug: string): void {
  const current = readCompareSet();
  if (current.includes(slug) || current.length >= MAX_COMPARE) return;
  writeCompareSet([...current, slug]);
}

/** Remove `slug` from the compare set. No-op if absent. */
export function removeFromCompare(slug: string): void {
  const current = readCompareSet();
  if (!current.includes(slug)) return;
  writeCompareSet(current.filter((s) => s !== slug));
}

/**
 * Toggle `slug` in the compare set. Returns whether the slug is in the set
 * afterward, so callers can tell "added" from "blocked" — adding is a no-op
 * (returns false) when the set is already at MAX_COMPARE and the slug isn't in
 * it.
 */
export function toggleCompare(slug: string): boolean {
  const current = readCompareSet();
  if (current.includes(slug)) {
    writeCompareSet(current.filter((s) => s !== slug));
    return false;
  }
  if (current.length >= MAX_COMPARE) return false;
  writeCompareSet([...current, slug]);
  return true;
}

/** Empty the compare set. */
export function clearCompare(): void {
  writeCompareSet([]);
}
