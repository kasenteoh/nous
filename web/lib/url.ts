// Shared URL helpers for source/citation rendering. One home for the http(s)
// host parse that the provenance affordances all need, so SourceLink, Sources,
// and the timeline coverage grouping can't drift.

/**
 * Display host for a parseable **http(s)** URL — lowercased, `www.`-stripped —
 * or null otherwise. Stricter than a bare `new URL()` (rejects `mailto:`,
 * `ftp:`, scheme-less bare domains, malformed): a source affordance must only
 * ever link to a real web source, never link nowhere (the moat's "every fact
 * sourced, no dead link").
 */
export function httpHost(url: string): string | null {
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    const host = u.hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return null;
  }
}
