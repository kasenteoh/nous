// Pure helpers for the /vs/[a]/[b] head-to-head compare pages. A pair is
// unordered — /vs/acme/globex and /vs/globex/acme are the same comparison — so
// every surface canonicalizes to one slug order (lexicographic). Keeping this
// pure and DB-free makes the ordering trivially testable and import-safe
// anywhere (no `server-only`).

/**
 * The two slugs in canonical (lexicographic) order. Both /vs URL orderings map
 * to this one pair, so the page can point its canonical tag + internal links at
 * a single URL and never split ranking signal across two mirror pages.
 */
export function canonicalVsPair(a: string, b: string): [string, string] {
  return a <= b ? [a, b] : [b, a];
}

/** The canonical /vs path for a pair, regardless of the order passed in. */
export function vsPath(a: string, b: string): string {
  const [x, y] = canonicalVsPair(a, b);
  return `/vs/${x}/${y}`;
}
