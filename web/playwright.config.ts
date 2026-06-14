import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright smoke harness for nous (Task B3 / Phase 7.2).
 *
 * Purpose: a release-blocking CI guard that the core routes still render and
 * that the Phase-1 web states can't silently regress:
 *   - pagination clamp (an out-of-range ?page= must NOT show the cold-start box)
 *   - excluded companies 404 by direct URL.
 *
 * Two run profiles, selected by environment (see e2e/smoke.spec.ts):
 *   - CI / secret-free: GitHub Actions `lint.yml` has no Supabase secrets, so
 *     every data query degrades to empty and `/c/<slug>` 404s. The structural
 *     checks (routes return 200/404, masthead + landmarks render) still hold
 *     and run unconditionally — this is the CI contract.
 *   - Local / data-backed: set SMOKE_HAS_DATA=1 (plus SMOKE_KNOWN_SLUG /
 *     SMOKE_EXCLUDED_SLUG) with web/.env.local pointing at prod PostgREST to
 *     additionally assert a known company renders and page=99999 is cold-start
 *     free. These extra assertions are skipped when SMOKE_HAS_DATA is unset.
 *
 * The webServer boots a production build (`next build && next start`) on a
 * fixed port so `npx playwright test` is self-contained.
 */

// Dedicated port so a developer's `next dev` on :3000 never clashes with the
// harness, and so CI is deterministic.
const PORT = Number(process.env.SMOKE_PORT ?? 3100);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  // Smoke routes are independent — run them in parallel for speed.
  fullyParallel: true,
  // No `.only` should ever reach CI.
  forbidOnly: !!process.env.CI,
  // A flaky route fetch (cold ISR render, free-tier Supabase wake-up) shouldn't
  // fail the gate on the first try; one retry locally, two in CI.
  retries: process.env.CI ? 2 : 1,
  // Single worker keeps the one shared Next server's load predictable; the
  // suite is tiny so parallel workers buy nothing.
  workers: 1,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  // Generous per-test ceiling: free-tier Supabase can cold-start, and the first
  // ISR render of a route compiles on demand.
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    // Build then serve a production bundle. `npm run build` also typechecks, so
    // a broken page fails here before any assertion runs.
    command: `npm run build && npm run start -- -p ${PORT}`,
    url: BASE_URL,
    // Reuse an already-running server locally (fast iteration); always boot a
    // fresh one in CI.
    reuseExistingServer: !process.env.CI,
    // Production build can be slow on a cold cache / CI runner.
    timeout: 180_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
