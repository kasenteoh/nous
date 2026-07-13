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
// Exact header row of /api/export (COLUMNS in app/api/export/route.ts) — the
// contract a VC's spreadsheet import depends on.
const CSV_HEADER =
  "name,slug,website,industry,hq_city,hq_state,latest_round_type," +
  "latest_round_amount_usd,latest_round_date,total_raised_usd," +
  "employees_min,employees_max,investors";
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

  test("/themes renders the themes index (200)", async ({ page }) => {
    // Secret-free CI has no themes data; the page must render its empty
    // state (not 500), and the chrome must mount — same contract as the
    // other index routes.
    const res = await page.goto("/themes");
    expect(res?.status(), "GET /themes status").toBe(200);
    await expectSiteChrome(page);
    await expect(
      page.getByRole("heading", { level: 1, name: "Themes" }),
    ).toBeVisible();
  });

  test("/themes/<unknown-slug> returns 404", async ({ page }) => {
    // Unknown theme slugs 404 by direct URL, with or without Supabase
    // (getThemeBySlug returns null in both cases).
    const res = await page.goto("/themes/definitely-not-a-theme-zzz");
    expect(res?.status(), "GET /themes/<unknown> status").toBe(404);
    await expect(
      page.getByRole("heading", { level: 1, name: /not found/i }),
    ).toBeVisible();
  });

  test("/industry renders the industries index (200)", async ({ page }) => {
    // Secret-free CI has no industry data; the page must render its empty
    // state (not 500), and the chrome must mount — same contract as /themes.
    const res = await page.goto("/industry");
    expect(res?.status(), "GET /industry status").toBe(200);
    await expectSiteChrome(page);
    await expect(
      page.getByRole("heading", { level: 1, name: "Industries" }),
    ).toBeVisible();
  });

  test("/industry/<non-canonical-slug> returns 404", async ({ page }) => {
    // Slugs are gated to canonical buckets (listCanonicalIndustries); an
    // unknown slug — and every slug when Supabase is unconfigured — 404s.
    const res = await page.goto("/industry/definitely-not-an-industry-zzz");
    expect(res?.status(), "GET /industry/<unknown> status").toBe(404);
    await expect(
      page.getByRole("heading", { level: 1, name: /not found/i }),
    ).toBeVisible();
  });

  test("/feed.xml serves a valid RSS document (200, xml content-type)", async ({
    request,
  }) => {
    // Secret-free CI yields an empty-but-valid feed (no items), not a 500.
    const res = await request.get("/feed.xml");
    expect(res.status(), "GET /feed.xml status").toBe(200);
    expect(
      res.headers()["content-type"] ?? "",
      "content-type",
    ).toContain("application/rss+xml");
    const body = await res.text();
    expect(body).toContain('<rss version="2.0"');
    expect(body).toContain("<channel>");
  });

  test("/vs/<a>/<a> (same company) returns 404", async ({ page }) => {
    // Comparing a company with itself is meaningless — loadVs returns null for
    // an identical pair regardless of data, so it 404s.
    const res = await page.goto("/vs/acme/acme");
    expect(res?.status(), "GET /vs/<a>/<a> status").toBe(404);
    await expect(
      page.getByRole("heading", { level: 1, name: /not found/i }),
    ).toBeVisible();
  });

  test("/vs/<unknown>/<unknown> returns 404", async ({ page }) => {
    // Two distinct slugs that aren't both listed (always the case with no
    // Supabase) → fewer than 2 companies resolve → 404.
    const res = await page.goto("/vs/unknown-co-aaa/unknown-co-bbb");
    expect(res?.status(), "GET /vs/<unknown>/<unknown> status").toBe(404);
    await expect(
      page.getByRole("heading", { level: 1, name: /not found/i }),
    ).toBeVisible();
  });

  test("/trends renders the funding trends dashboard (200)", async ({
    page,
  }) => {
    // Secret-free CI has no funding data; the page must render (empty chart +
    // omitted sections), not 500, with the chrome mounted.
    const res = await page.goto("/trends");
    expect(res?.status(), "GET /trends status").toBe(200);
    await expectSiteChrome(page);
    await expect(
      page.getByRole("heading", { level: 1, name: "Funding trends" }),
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

  test("/companies?q= under the default sort stays a valid page (200)", async ({
    page,
  }) => {
    // Exercises the semantic-search path (E-2): a bare q with no explicit
    // sort is the one shape that attempts query embedding + the blend. In
    // secret-free CI every semantic sub-step is allowed to fail (no model,
    // no Supabase) and the page must still degrade to lexical and 200 —
    // exactly the embedQuery-null → listCompaniesHybrid-pure-lexical path.
    const res = await page.goto("/companies?q=ai+for+logistics");
    expect(res?.status(), "GET /companies?q= status").toBe(200);
    await expectSiteChrome(page);
  });

  test("/companies with the full filter querystring stays a valid page (200)", async ({
    page,
  }) => {
    // Every filter param at once must never crash the parser — with no data
    // the result set is just empty, which is fine structurally.
    const res = await page.goto(
      "/companies?q=zzz&industry=Fintech&source=techcrunch&stage=Series+A" +
        "&funded_since_days=30&min_raised=1000000&max_raised=90000000" +
        "&founded_after=2019&founded_before=2026&emp_min=5&emp_max=500&sort=funding_desc",
    );
    expect(res?.status(), "filtered /companies status").toBe(200);
    await expectSiteChrome(page);
  });

  test("/compare renders its empty state (200)", async ({ page }) => {
    const res = await page.goto("/compare");
    expect(res?.status(), "GET /compare status").toBe(200);
    await expectSiteChrome(page);
    await expect(
      page.getByRole("heading", { level: 1, name: "Compare companies" }),
    ).toBeVisible();
    await expect(
      page.getByText("No companies selected to compare."),
    ).toBeVisible();
  });

  test("/compare with unknown slugs stays a valid page (200)", async ({
    page,
  }) => {
    // Unknown (or, secret-free, ALL) slugs resolve to no listed companies;
    // the page must degrade to its explanatory empty state, never 500.
    const res = await page.goto("/compare?slugs=zzz-nope-a,zzz-nope-b");
    expect(res?.status(), "GET /compare?slugs=… status").toBe(200);
    await expectSiteChrome(page);
    await expect(
      page.getByText("None of those companies are listed."),
    ).toBeVisible();
  });

  test("/api/export responds deliberately: 200 CSV with data, 503 without a backend", async ({
    page,
  }) => {
    // Secret-free CI has no Supabase env, so the route answers a deliberate
    // 503 (plain text) rather than crashing; with credentials it streams CSV.
    // Both are correct — what must never happen is an unhandled 500.
    const res = await page.request.get("/api/export");
    expect([200, 503], "GET /api/export status").toContain(res.status());
    if (res.status() === 200) {
      expect(res.headers()["content-type"]).toContain("text/csv");
      const firstLine = (await res.text()).split("\n")[0];
      expect(firstLine).toBe(CSV_HEADER);
    } else {
      expect(res.headers()["content-type"]).toContain("text/plain");
    }
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

  test("journey: browse → filter → company page → compare → CSV export", async ({
    page,
  }) => {
    // Generous ceiling: five navigations against free-tier Supabase.
    test.setTimeout(90_000);

    // 1 — Browse: the card grid renders.
    await page.goto("/companies");
    const cardNames = page.locator('main a[href^="/c/"] h2');
    await expect(cardNames.first()).toBeVisible();
    // Prefer a name without characters the ilike sanitizer strips (%, *, (),
    // commas) — searching for e.g. "Acme (AI)" can't round-trip exactly.
    const names = (await cardNames.allInnerTexts()).map((n) => n.trim());
    const companyName =
      names.find((n) => /^[a-z0-9 .&'-]+$/i.test(n)) ?? names[0];

    // 2 — Filter: searching for that exact name must keep the company in the
    // (server-filtered) result set.
    const searchBox = page.getByPlaceholder("Search companies…");
    await searchBox.fill(companyName);
    await searchBox.press("Enter");
    await expect(page).toHaveURL(/[?&]q=/);
    const filteredCard = page
      .locator('main a[href^="/c/"]')
      .filter({ hasText: companyName })
      .first();
    await expect(filteredCard).toBeVisible();

    // 3 — Company page: the filtered card links to a live profile.
    await filteredCard.click();
    await expect(page).toHaveURL(/\/c\/[^/]+$/);
    const companySlug = new URL(page.url()).pathname.split("/").pop() ?? "";
    expect(companySlug).not.toBe("");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByText(/Discovered via/i)).toBeVisible();

    // 4 — Compare: tick two cards' Compare checkboxes, follow the sticky bar.
    // After a check the toggle's accessible name flips to "Remove … from
    // compare", so first() always targets the next still-unchecked card.
    await page.goto("/companies");
    const addToggles = page.getByRole("checkbox", {
      name: /^Add .+ to compare$/,
    });
    await addToggles.first().check();
    await addToggles.first().check();
    const bar = page.getByRole("region", { name: "Compare selection" });
    await expect(bar).toBeVisible();
    await bar.getByRole("link", { name: /^Compare 2/ }).click();
    await expect(page).toHaveURL(/\/compare\?slugs=/);
    await expect(
      page.getByRole("heading", { level: 1, name: "Compare companies" }),
    ).toBeVisible();
    // Both companies appear as linked column headers of the comparison table.
    await expect(page.locator('thead a[href^="/c/"]')).toHaveCount(2);

    // 5 — CSV export: same filter as step 2, exact header row, and the
    // filtered company's row present.
    const res = await page.request.get(
      `/api/export?q=${encodeURIComponent(companyName)}`,
    );
    expect(res.status(), "GET /api/export status").toBe(200);
    expect(res.headers()["content-type"]).toContain("text/csv");
    expect(res.headers()["content-disposition"]).toContain(
      'attachment; filename="nous-companies.csv"',
    );
    const body = await res.text();
    expect(body.split("\n")[0]).toBe(CSV_HEADER);
    expect(body).toContain(`"${companySlug}"`);
  });
});
