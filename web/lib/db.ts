// Build-time boundary guard: importing this module from any client-component
// graph fails the build (the `server-only` package throws outside a
// react-server environment). See web/AGENTS.md "Server-only boundary".
import "server-only";

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Server-only Supabase client. The service role key bypasses RLS, so this must
// never be imported or instantiated from a client component or shipped to the browser.
export function createSupabaseServerClient(): SupabaseClient {
  const url = process.env.SUPABASE_URL;
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

  if (!url || !serviceRoleKey) {
    throw new Error(
      "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in the server environment",
    );
  }

  return createClient(url, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}
