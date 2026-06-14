import { test, expect, type Page } from "@playwright/test";

/**
 * Release-blocking smoke harness (Task B3 / Phase 7.2).
 *
 * Split by data availability so the SAME spec is the CI gate AND a richer local
 * check (see playwright.config.ts header):
 *
 *   - Structural block — runs ALWAYS. Routes return the right status (200/404)
 *     and the site chrome (masthead nav, page landmark) renders. Needs no
 *     Supabase data, so it is the GitHub Actions contract (lint.yml has no
 *     secrets → all data queries degrade to empty / 404).
 *
 *   - Data-backed block — runs only when SMOKE_HAS_DATA=1. Asserts a known
 *     company renders and that an out-of-range ?page= clamps WITHOUT showing the
 *     cold-start box. These are inherently data-dependent (an empty catalog is
 *     *correctly* cold-start), so they're skipped in secret-free CI and run
 *     locally against prod PostgREST via web/.env.local.
 */

// Exact cold-start copy from app/companies/page.tsx — the Phase-1 regression we
// guard against. If either string ever reappears on a clamped/out-of-range
// page, the pagination-clamp logic has regressed.
const COLD_START_PROSE = "Run the discovery pipeline";
const COLD_START_CMD = "nous refresh-vc-portfolios";

const HAS_DATA = process.env.SMOKE_HAS_DATA === "1";
// Slugs are injected at runtime (never committed). Fallbacks keep the excluded
// check meaningful even if the env var is unset: an excluded OR nonexistent
// slug both 404, which is exactly the assertion.
const KNOWN_SLUG = process.env.SMOKE_KNOWN_SLUG ?? "";
const EXCLUDED_SLUG =
  process.env.SMOKE_EXCLUDED_SLUG ?? "definitely-not-a-real-company-zzz";

/**
 * Every page shares the masthead. Asserting it renders proves the route both
 * returned HTML and mounted the root layout (not an error boundary / blank
 * body), independent of any database content.
 */
async function expectSiteChrome(page: Page): Promise<void> {
  const primaryNav = page.getByRole("navigation", { name: "Primary" });
  await expect(primaryNav).toBeVisible();
  await expect(primaryNav.getByRole("link", { name: "Browse" })).toBeVisible();
  await expect(
    primaryNav.getByRole("link", { name: "Investors" }),
  ).toBeVisible();
  // The content landmark is always present (layout wraps children in it).
  await expect(page.locator("main").first()).toBeVisible();
}

test.describe("structural smoke (no data required — CI contract)", () => {
  test("/ renders the home landmark + chrome (200)", async ({ page }) => {
    const res = await page.goto("/");
    expect(res?.status(), "GET / status").toBe(200);
    await expectSiteChrome(page);
    // Stable, always-rendered home heading (sr-only <h1> in app/page.tsx).
    await expect(
      page.getByRole("heading", {
        level: 1,
        name: /US software startup discovery/i,
      }),
    ).toBeVisible();
  });

  test("/companies renders the browse page (200)", async ({ page }) => {
    const res = await page.goto("/companies");
    expect(res?.status(), "GET /companies status").toBe(200);
    await expectSiteChrome(page);
    // The browse hero <h1> is "nous"; the page's own search form is always
    // present. Target it by placeholder — the masthead also has a "Search"
    // box with the same aria-label, so a role+name lookup is ambiguous.
    await expect(
      page.getByRole("heading", { level: 1, name: "nous" }),
    ).toBeVisible();
    await expect(
      page.getByPlaceholder("Search companies…"),
    ).toBeVisible();
  });

  test("/companies?page=99999 stays a valid browse page (200)", async ({
    page,
  }) => {
    // Out-of-range page must clamp, never 500.
    const res = await page.goto("/companies?page=99999");
    expect(res?.status(), "GET /companies?page=99999 status").toBe(200);
    await expectSiteChrome(page);
    await expect(
      page.getByRole("heading", { level: 1, name: "nous" }),
    ).toBeVisible();
  });

  test("/investors renders the investor index (200)", async ({ page }) => {
    const res = await page.goto("/investors");
    expect(res?.status(), "GET /investors status").toBe(200);
    await expectSiteChrome(page);
    await expect(
      page.getByRole("heading", { level: 1, name: "Investors" }),
    ).toBeVisible();
  });

  test("/surprise resolves to a 200 page (redirect target)", async ({
    page,
  }) => {
    // /surprise is a force-dynamic redirect: to /c/<random> when data exists,
    // else to /companies. Either way the FINAL document must be 200 and carry
    // the site chrome. We don't assert the destination path (it's random / data
    // dependent), only that the redirect lands somewhere live.
    const res = await page.goto("/surprise");
    expect(res?.status(), "GET /surprise final status").toBe(200);
    await expectSiteChrome(page);
    expect(new URL(page.url()).pathname).not.toBe("/surprise");
  });

  test("/c/<excluded-or-unknown-slug> returns 404", async ({ page }) => {
    // Excluded companies (and nonexistent slugs) must 404 by direct URL — junk
    // pages never render. Holds with or without Supabase: getCompanyBySlug
    // returns null for excluded rows AND when unconfigured.
    const res = await page.goto(`/c/${EXCLUDED_SLUG}`);
    expect(res?.status(), `GET /c/${EXCLUDED_SLUG} status`).toBe(404);
    // The custom not-found body renders inside the normal layout.
    await expect(
      page.getByRole("heading", { level: 1, name: /not found/i }),
    ).toBeVisible();
  });
});

test.describe("data-backed smoke (SMOKE_HAS_DATA=1 — local / prod-backed)", () => {
  test.skip(
    !HAS_DATA,
    "needs Supabase data (set SMOKE_HAS_DATA=1 with prod creds); secret-free CI runs the structural block only",
  );

  test("/companies?page=99999 clamps WITHOUT the cold-start box", async ({
    page,
  }) => {
    const res = await page.goto("/companies?page=99999");
    expect(res?.status()).toBe(200);
    // The core Phase-1 guard: a populated catalog must clamp an out-of-range
    // page to the last real page, never fall back to the empty-catalog prompt.
    const body = await page.locator("body").innerText();
    expect(body).not.toContain(COLD_START_PROSE);
    expect(body).not.toContain(COLD_START_CMD);
    // And it must actually show companies (clamped to a real page), proving the
    // 200 isn't an empty shell.
    await expect(page.locator("main h2").first()).toBeVisible();
  });

  test("/c/<known-slug> renders the company profile (200)", async ({
    page,
  }) => {
    expect(KNOWN_SLUG, "SMOKE_KNOWN_SLUG must be set when SMOKE_HAS_DATA=1").not.toBe(
      "",
    );
    const res = await page.goto(`/c/${KNOWN_SLUG}`);
    expect(res?.status(), `GET /c/${KNOWN_SLUG} status`).toBe(200);
    await expectSiteChrome(page);
    // Company name is the page <h1>; "Discovered via …" is on every profile.
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByText(/Discovered via/i)).toBeVisible();
  });
});
