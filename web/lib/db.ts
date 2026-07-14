// Build-time boundary guard: importing this module from any client-component
// graph fails the build (the `server-only` package throws outside a
// react-server environment). See web/AGENTS.md "Server-only boundary".
import "server-only";

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Thrown when Supabase env is missing/partial in an environment where that is
// a deployment mistake rather than an expected secret-free run. Query helpers
// rethrow this (→ the page 500s loudly) instead of degrading to "empty
// catalog", which used to make a prod misconfig look like a 404-everywhere
// site instead of an error (W-C.2).
export class SupabaseConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SupabaseConfigError";
  }
}

// Where is a missing config a deployment mistake? Exactly on Vercel — the one
// place this app is deployed (`VERCEL` is set at build and runtime there).
// Everywhere else, running without Supabase is a supported mode that must
// degrade to empty results:
//   - lint.yml builds and runs the Playwright smoke with no secrets (and
//     plants a canary SUPABASE_SERVICE_ROLE_KEY *without* SUPABASE_URL for the
//     bundle scan, so a PARTIAL config off-Vercel must stay benign too);
//   - local dev without .env.local browses an empty catalog.
function missingConfigIsDeploymentMistake(): boolean {
  return !!process.env.VERCEL;
}

// Server-only Supabase client. The service role key bypasses RLS, so this must
// never be imported or instantiated from a client component or shipped to the browser.
//
// Throws SupabaseConfigError (loud — callers must NOT swallow it) on Vercel
// with missing/partial env; throws a plain Error (benign — callers degrade to
// empty) elsewhere.
export function createSupabaseServerClient(): SupabaseClient {
  const url = process.env.SUPABASE_URL;
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

  if (!url || !serviceRoleKey) {
    const missing = [
      !url && "SUPABASE_URL",
      !serviceRoleKey && "SUPABASE_SERVICE_ROLE_KEY",
    ]
      .filter(Boolean)
      .join(" and ");
    if (missingConfigIsDeploymentMistake()) {
      throw new SupabaseConfigError(
        `${missing} not set in the Vercel environment — the site cannot read ` +
          "any data. Add the env var(s) in the Vercel project settings.",
      );
    }
    throw new Error(
      "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in the server environment",
    );
  }

  return createClient(url, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}

/**
 * True when the Supabase server env is fully present. Feed route handlers use
 * this to tell "Supabase intentionally absent" (secret-free CI/local build) —
 * degrade to an empty-but-valid feed, never 404/500 — apart from "entity
 * genuinely not found" — a truthful 404. Mirrors the env check in
 * {@link createSupabaseServerClient} without constructing a client or throwing.
 */
export function isSupabaseConfigured(): boolean {
  return Boolean(
    process.env.SUPABASE_URL && process.env.SUPABASE_SERVICE_ROLE_KEY,
  );
}
