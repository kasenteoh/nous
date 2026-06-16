// Site identity helpers — pure, dependency-free, safe to import anywhere
// server-side (layout metadata, sitemap/robots routes, OG-image routes).

export const SITE_NAME = "nous";

const REPO_URL = "https://github.com/kasenteoh/nous";

/**
 * Canonical origin for absolute URLs (sitemap, robots, JSON-LD, metadataBase).
 *
 * Resolution order:
 *   1. NEXT_PUBLIC_SITE_URL — explicit override, set once a custom domain exists.
 *   2. VERCEL_PROJECT_PRODUCTION_URL — Vercel-provided production hostname
 *      (no protocol, so https:// is prepended).
 *   3. http://localhost:3000 — local dev / CI builds without env.
 */
export function siteOrigin(): string {
  const explicit = process.env.NEXT_PUBLIC_SITE_URL;
  if (explicit) return explicit;

  const vercel = process.env.VERCEL_PROJECT_PRODUCTION_URL;
  if (vercel) return `https://${vercel}`;

  return "http://localhost:3000";
}

/**
 * Prefilled "new GitHub issue" URL for the "Report incorrect data" link
 * in the site footer. The repo is public, so these links resolve for visitors.
 */
export function repoIssueUrl(title: string, body: string): string {
  return `${REPO_URL}/issues/new?title=${encodeURIComponent(title)}&body=${encodeURIComponent(body)}`;
}
