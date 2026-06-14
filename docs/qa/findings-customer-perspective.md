# nous — Customer-Perspective QA Findings

**Date:** 2026-06-13
**Target:** local `npm run dev` (`http://localhost:3000`) reading **live production Supabase data** (~1,270 companies, ~85 investors, 18 states, ~5,200 tags). This is exactly what a customer sees, against real data.
**Method:** Black-box, customer's-eye testing. A 16-area agent fan-out drove the site over HTTP (the site is almost entirely server-rendered, so the HTML a browser receives is fully exercisable), each finding independently re-verified by a second agent. A live **Claude-in-Chrome** pass covered browser-only dimensions (theme, console/network, interactivity, focus). Gaps left by the agent run were closed by hand with `curl`.
**Database:** never queried directly (per constraint). No app code was modified.

> **Run caveat:** the Anthropic account hit its **monthly spend limit** partway through, killing 2 of 16 agent lanes (`link-crawl` finder and `data-trust` verifier). I reproduced both by hand with `curl`, so coverage is complete — but the dedicated data-trust *re-verification* pass is partial (see Coverage).

---

## Resolution status (updated 2026-06-14)

Fixes applied on branch `qa/customer-findings` (build + lint pass). Some findings were already resolved on the active remediation branch; those were left untouched.

- **Fixed this pass (frontend, verified):** H9 (related/competitor links now drop excluded companies), H6 (investor portfolio unions funded companies — no more "no portfolio" beside funding activity), H7 (`/new` count filtered to match the list + self-canonical), H8 (`/new` single title suffix), H3 (display guard drops competitor entries that leak LLM scratch notes — *stopgap; see below*), M5 (`/location/<code>` case-insensitive), M6 (branded 404 — also fixes the chrome-less tag/location 404s, L9), M8 (stable `sr-only` homepage `<h1>`; spotlight → `<h2>`), M10 (visible focus indicators), G1 (footer with disclaimer + **"Report an error"** link, closing the no-feedback gap), G7 (skip-to-content link), M3 (location links in footer), L13 (nav `aria-label`).
- **Already fixed on the remediation branch (verified, untouched):** C1 / H4 / H5 (investor ranking via denormalized `portfolio_count`, migration 0025), H1 (pagination CLI-command leak), M2 (dead source-filter options), L3 (raw `discovered_via` enum).
- **Fixed this pass (pipeline extraction root-cause):** H2 (company-description prompt + `CompanyDescription` validator drop testimonial-derived rosters — the "3× Co-Founder, COO" class), H3 root (competitor-analysis prompt + `CompetitorAnalysis` validator strip selection scratch-notes). These fix the *source*, not individual companies; existing rows clean up on the next enrich/analyze run, and the web display guard covers H3 immediately.
- **Still open — needs a data backfill or a product decision:** M1 (industry-taxonomy normalization), M4 (funding-coverage backfill), G2 (thin profiles), G4 / G5 (investor website + description data), L4 (Bron help-center URL), M9 (label individual investors). The broader data-quality findings — parked/for-sale domains, wrong websites, fabricated funding dates, non-US masking, placeholder names — were already addressed by the remediation effort (#59–#84). Minor cosmetic Lows (L1/L2/L5/L6/L7/L10/L11/L12) deferred.

---

## Executive summary

The site looks polished and is technically solid where it counts — clean console, zero failed network requests, fast server-rendered pages, working search/theme/carousel, and a strict no-fabrication discipline that renders "—" instead of inventing numbers. **The problems are not crashes; they are broken promises and self-contradicting data** that a discerning visitor (the exact audience for a VC/startup discovery site) will notice and lose trust over.

The ten things to fix first:

1. **🔴 The Investors directory is not ranked at all.** It says "ranked by portfolio size" directly under the title, but it's sorted **alphabetically**. Page 1 is dominated by firms showing "0 companies"; the single biggest investor, **Y Combinator (1,002 companies), is on the last page.** (C1)
2. **🟠 Most investors contradict themselves.** ~84% of list rows say "0 companies", yet those same firms' pages list real funded companies. On a firm's own page, "Portfolio: 0 companies indexed / No portfolio companies recorded yet" sits directly above a funding table full of real companies. (H5, H6)
3. **🟠 The same metric shows two different numbers.** Andreessen Horowitz reads **852** companies on the list but **696** on its own page; Lightspeed 588 vs 492. (H4)
4. **🟠 Fabricated-looking data on flagship pages.** Shippo's "Leadership" is **12 testimonial/customer names** posing as executives (three different "Co-Founder, COO"s). (H2)
5. **🟠 An LLM's internal note is shown to customers.** Assembly's competitor list includes "Den" with the rationale: *"Included temporarily for evaluation but should be dropped."* (H3)
6. **🟠 An internal developer command leaks to the public.** Visiting `/companies?page=99999` (or one click past the last page) shows a fake "No companies indexed yet" screen printing the ops command **`nous refresh-vc-portfolios`**. (H1)
7. **🟠 Related/competitor links lead to dead 404s.** Company pages link to peer companies that don't exist — Harvey→Wevorce, Assembly→Brinc, Bron→Yellow, plus Stripe/Plaid/Nubank/Oportun/Perfios. (H9)
8. **🟠 The "New this week" feed miscounts itself.** It claims "76 rounds extracted" but lists 70; the homepage repeats the wrong number. Its browser-tab title is also doubled: "New this week — nous — nous". (H7, H8)
9. **🟡 The primary discovery filter is broken vocabulary.** The Industry dropdown has **251 freeform values** with duplicates/casing variants (`ad-tech`/`adtech`/`advertising technology`; `climate tech`/`climate-tech`/`cleantech`), so each filter hides matches under its synonyms. Two Source options ("News", "Unknown") match **zero** companies. (M1, M2)
10. **🟡 Funding — the headline fact — is blank for marquee companies** (Upstart, Harvey, Shippo, Swan, Assembly all show "Total raised —"), and there is **no footer and no way to report a wrong fact** anywhere on the site. (M4, G1)

**Confirmed findings:** 1 Critical · 10 High · 10 Medium · 8 Gap · ~17 Low.
**Works well:** server rendering (no client-side errors), search injection-safety, theme persistence, `/surprise`, special-character tag encoding, per-state pages — see [What works well](#what-works-well).

---

## 🔴 Critical

### C1 — The Investors directory is sorted alphabetically, not "by portfolio size" as it claims
**Where:** `/investors` (and `?page=2`)
**Customer sees:** Right under the "Investors" heading it reads *"85 firms, ranked by portfolio size."* In reality the rows are in pure alphabetical order. Page 1 opens with `1517 Fund` (0), `53 Stations` (0), `8i Ventures` (0), `8VC` (0), `Abstract` (0)… — a wall of "0 companies" firms. The most important investors are scattered or buried: **Y Combinator (1,002 companies) is on the final page.** A visitor who comes to find the most active investors gets the opposite.
**Evidence:** `names == sorted(names)` is true across both pages; page-1 company-count sequence is `0,0,0,0,0,152,0,0,0,852,0,0…` (not descending — 11 of 49 adjacent pairs violate descending order). Combined with H5 (counts are wrong anyway), the ordering is doubly broken.
**Fix:** order by the real portfolio size (the same count the detail page uses — see H4/H5) descending; the subtitle then becomes true.

---

## 🟠 High

### H1 — Out-of-range pages show a fake "empty database" dead-end, and `/companies` leaks an internal CLI command
**Where:** `/companies?page=99999` (any unfiltered page past the last, e.g. `?page=44`); `/investors?page=3`, `?page=9999`
**Customer sees:** A stale/shared deep link, or clicking "Next" once past the end, lands on a screen stating the **entire site has no data** — even though ~1,270 companies and 85 investors exist. On `/companies` it renders "No companies indexed yet." followed by the developer instruction **`nous refresh-vc-portfolios`** in a code block. On `/investors` the header flips to "**0 firms**, ranked by portfolio size. No investors indexed yet." Both have no pagination and no way back.
**Evidence:** `GET /companies?page=99999` → HTTP 200, 0 cards, no "Showing…" header, no pagination nav; body contains `No companies indexed yet` and `nous refresh-vc-portfolios`. `GET /investors?page=9999` → 200, body 21KB vs ~70KB, text "Investors 0 firms … No investors indexed yet."
**Fix:** clamp `page` to `[1, lastPage]` (redirect or render the last page); never show the first-run empty state when the catalog is non-empty; the CLI hint belongs only behind a genuinely empty table.

### H2 — Shippo's "Leadership" lists 12 bogus people (testimonial/customer names as executives)
**Where:** `/c/shippo`
**Customer sees:** A fabricated-looking executive roster — three different people each titled **"Co-Founder, COO"**, two cards holding two names joined by "+", and three separate people titled **CEO**. Shippo's actual founders are absent. It reads as scraped from a testimonials/customers page.
**Evidence:** Parsed the Leadership section: 12 name/role pairs incl. Tiffany Jones / Co-Founder, COO; Nancey Harris / Co-Founder, COO; Wendy Webster / Co-Founder, COO; multi-name cards; 3 CEO cards.
**Fix:** people-extraction is mis-firing on non-leadership pages; gate on confidence and drop multi-name / duplicate-title rosters. This is the kind of error that most damages a data product's credibility.

### H3 — Assembly's competitor list leaks the LLM's internal scratch note to customers
**Where:** `/c/assembly`
**Customer sees:** Competitor #6 "Den" with rationale text that literally says it isn't a competitor: *"…Included temporarily for evaluation but should be dropped."* The visitor reads, verbatim, that nous knowingly shows a non-competitor.
**Evidence:** Rendered verbatim in the Competitors section: "Den #6 potential competitor (AI-inferred) … Included temporarily for evaluation but should be dropped."
**Fix:** the competitor prompt's meta-commentary is being persisted and rendered; strip/avoid model scratch notes and validate competitor entries before storing.

### H4 — The same investor shows two different portfolio sizes (list vs. detail)
**Where:** `/investors` vs `/investor/<slug>`
**Customer sees:** Andreessen Horowitz: **852** companies on the list, **696** on its own page (off by 156). Lightspeed: **588** vs **492**. Y Combinator: **1,002** vs a smaller detail number. A visitor can't tell which figure is real, which undermines every number on the site.
**Fix:** both views must compute portfolio size the same way (one query/definition).

### H5 — Firms with real funding activity are shown as "0 companies" on the list (most rows)
**Where:** `/investors` (e.g. 1517 Fund, 53 Stations, Abstract) vs their detail pages
**Customer sees:** ~69 of 82 rows read "0 companies", making the directory look empty/worthless — and self-contradicting: "1517 Fund — 0 companies" on the list, but its page shows a real round ("Astrus — $8M, participant").
**Fix:** the list-page count is using a different (and wrong) relationship than the detail page; unify with H4.

### H6 — Investor pages contradict themselves: "0 companies / no portfolio" above a table of real companies
**Where:** `/investor/valor-equity-partners` and most others (contrary, transformation-capital, 1517-fund, shopify-ventures, plug-and-play, f-prime, betaworks, …)
**Customer sees:** "Portfolio size **0 companies indexed**" and "No portfolio companies recorded yet." rendered directly above a **Funding activity** table listing real companies (Valor → Loop, Series C, $95M, lead). Two halves of one page flatly disagree.
**Fix:** populate the portfolio from the same data that drives funding activity (or derive one from the other).

### H7 — "New this week" overstates its own round count (and the homepage repeats it)
**Where:** `/new` (and the homepage margin note)
**Customer sees:** "198 companies discovered · **76 rounds** extracted in the last 7 days", but only **70** rounds are actually listed below (8 under June 13 + 62 under June 12). The list is well under its 200 cap, so nothing is hidden — the number is just wrong. The homepage shows the same inflated figure.
**Fix:** the summary count and the rendered list use different queries/》windows; make the count match what's shown.

### H8 — `/new` has a doubled title: "New this week — nous — nous"
**Where:** `/new` `<title>`, `og:title`, `twitter:title`
**Customer sees:** The browser tab, Google result, and every social share preview for a nav-linked page read "New this week — nous — nous".
**Evidence:** `<title>New this week — nous — nous</title>` (siblings are correct, e.g. "Browse — nous").
**Fix:** `/new` sets a full title string while the root layout also appends " — nous"; return the bare "New this week" like other pages.

### H9 — Related/competitor links lead to dead 404 pages
**Where:** company "Related companies" / "Competitors" sections — confirmed Harvey→`/c/wevorce`, Assembly→`/c/brinc`, Bron→`/c/yellow`; also `/c/stripe`, `/c/plaid`, `/c/nubank`, `/c/oportun`, `/c/perfios` referenced elsewhere
**Customer sees:** Clicking a related/competitor company (e.g. on Harvey, the "Wevorce — legal tech / Both in legal tech" card) lands on a hard 404. An internal-link crawl of 1,506 unique links found **8 distinct company links that 404**.
**Evidence:** all 8 `/c/<slug>` return 404; their slugs are linked from live company pages. Every *other* internal link (1,498) returned 200.
**Fix:** only link a related/competitor company when a page for it actually exists; otherwise render its name as plain text (the way some investor names already are).

### H10 — (grouped) Investor pages are pervasively contradictory — see H4/H5/H6
The investor surface is the weakest area of the site: ranking (C1), list counts (H5), list-vs-detail mismatch (H4), and detail self-contradiction (H6) are all live simultaneously. Treat them as one workstream — they share a root cause in how investor↔company relationships are counted and joined.

---

## 🟡 Medium

### M1 — Industry filter is a 251-value freeform vocabulary with heavy duplication
**Where:** `/companies` Industry dropdown
**Customer sees:** 251 options, many of them the same concept split apart: `ad-tech` / `adtech` / `advertising technology`; `biotech` / `biotechnology`; `climate tech` / `climate-tech` / `cleantech` / `clean energy`; `AI infrastructure` / `AI research` / `AI hardware` / `AI productivity`; many casing/punctuation dupes. Filtering by one variant hides companies filed under the others (`ad-tech` shows 1; more sit under `adtech`). The homepage even advertises "+245 more" industries.
**Fix:** normalize to a curated, deduped industry taxonomy (map freeform LLM output → canonical set); collapse casing/synonyms.

### M2 — Two Source filter options ("News", "Unknown") match zero companies
**Where:** `/companies?source=news`, `?source=unknown`
**Customer sees:** Selecting either always yields "No companies match these filters." Two of the four discovery-source options are dead, making the filter look broken.
**Evidence:** both render the chosen `<option … selected>` and an empty results box. (Only `vc_portfolio` and `techcrunch` return results.)
**Fix:** drop options that can never match, or fix the underlying `discovered_via` values so "news" maps to real rows.

### M3 — State/location pages exist and work, but are completely undiscoverable
**Where:** `/location/<ST>`
**Customer sees:** Working, paginated per-state pages (e.g. `/location/CA` → 113 companies, "Page 1 of 4"), but **no link to them anywhere** — not in the masthead, not on the homepage, not on company pages' headers when… (note: company headers *do* link the location when a state is known, but thin pages omit it — see G2). A visitor wanting "startups in California" can't get there by clicking.
**Fix:** add a browse-by-location entry point (and ensure company headers link location consistently).

### M4 — Funding is blank on marquee companies (Total raised "—", "No funding rounds recorded yet")
**Where:** `/c/upstart`, `/c/harvey`, `/c/shippo`, `/c/swan`, `/c/assembly` (and others)
**Customer sees:** On a *funding* discovery site, the most-expected fact is blank for famous, heavily-funded companies. Upstart's own About text says it "originated more than $57 billion in loans" yet "Total raised —". Not a render bug (funding shows correctly where data exists, e.g. Helion $465M) — it's coverage. The no-fabrication policy is correct; the *gap* is the issue.
**Fix:** widen funding-news coverage / backfill rounds for high-profile companies; consider a softer empty state than the bare "—".

### M5 — Case-sensitivity trap: `/location/ca` 404s while `/location/CA` works
**Where:** `/location/ca`
**Customer sees:** A lowercased state code (mobile keyboards, hand-edited or shared links) hits a hard 404 even though the state has 113 companies one capitalization away.
**Fix:** normalize the state segment to uppercase before lookup (or 308-redirect lowercase→uppercase).

### M6 — Bad URLs render Next.js's unstyled default 404, not a branded page
**Where:** `/totally/unknown/path`, `/location/ZZ`, `/c/`, `/tag/`, empty slugs
**Customer sees:** Correct HTTP 404, but the body is a bare black-on-white "404 / This page could not be found." in system font with tab title "404: This page could not be found." — looks like a framework error, not nous. (Company and investor *slug* 404s do better; the global + facet 404s don't.)
**Fix:** add a root `app/not-found.tsx` with the nous masthead + a link home/browse.

### M7 — `/new` emits no `<link rel="canonical">` (every other primary page has one)
**Where:** `/new`
**Customer sees:** (SEO) the nav-linked feed can be indexed under duplicate URL variants, splitting ranking signals. It's the only primary page missing a canonical.
**Fix:** add `alternates: { canonical: "/new" }` to its metadata.

### M8 — The homepage's only `<h1>` is a rotating company name inside an aria-live region
**Where:** `/` (spotlight)
**Customer sees:** (Screen-reader) heading navigation lands on an arbitrary company name ("Fresha") as the page's top heading instead of anything identifying nous; as the carousel rotates, the h1 text changes under `aria-live="polite"`.
**Fix:** give the page a stable visually-hidden `<h1>` ("nous — US software startup discovery") and demote the spotlight name to `<h2>`/`<p>`.

### M9 — Individual people appear as standalone "investor" pages with no indication they're people
**Where:** `/investor/raymond-chik` (also pradeep-sindhu, anton-osika)
**Customer sees:** A bare person's name framed like a firm — no type badge, no description, "0 companies indexed", one undated row. Looks like junk/garbled data.
**Fix:** tag angel/individual investors distinctly (or a "Person" badge), and don't render them identically to firms.

### M10 — Weak/missing visible keyboard-focus indicators (browser-verified)
**Where:** masthead search + nav links, site-wide
**Customer sees:** (Keyboard/low-vision) the masthead search sets `focus:outline-none` and only changes its border subtly on focus; nav links show no clear focus ring when tabbed to. Hard to tell where keyboard focus is.
**Fix:** add a visible `focus-visible` ring to interactive controls; don't remove outlines without a replacement.

---

## 🟢 Gaps (expected things that are missing)

| # | Gap | Where | Why it matters |
|---|-----|-------|----------------|
| G1 | **No footer anywhere** — no copyright, legal/terms, methodology, contact, **or any way to report a wrong fact**. | every page | Reads as unfinished; and given the data errors above (H2/H3), customers have **zero recourse** to flag them. `repoIssueUrl()` exists in code but is unused. |
| G2 | Thin company pages omit **HQ location, founding year, AND employee count** together. | Upstart, Harvey, Shippo, Swan, Assembly | Basic profile context absent even for major companies; headers read sparse (just site + industry + date). |
| G3 | **Tag sprawl** — 5,199 indexable tag pages for ~1,270 companies (4:1). ~64% of sampled tags have ≤1 company. | `/sitemap.xml`, e.g. `/tag/cobol`, `/tag/mixnet` | Thousands of thin pages dilute SEO and bury useful multi-company tags. No tag-browse entry point exists either (`/tag` 404s). |
| G4 | **No investor website link** on any investor page (incl. Accel, Greylock). | all `/investor/*` | Company pages link out to sites; investor pages don't, so you can't reach the firm. |
| G5 | **No investor descriptions** — even major firms get no blurb. | all `/investor/*` | Company pages are rich; investor pages teach you nothing about the firm. |
| G6 | **No JSON-LD** on investor / tag / location / listing pages (only home + company have it). | `/investor/*`, `/tag/*`, `/location/*`, `/companies` | Misses rich-result eligibility for entity pages. |
| G7 | **No skip-to-content link** on any page. | all | Keyboard/SR users must tab through the full masthead (8 controls) on every page; `<main>` has no id to target. |
| G8 | **No mobile masthead search input** — collapses to a `⌕` link to `/companies` (source-confirmed; couldn't render <768px here). | all, mobile | Extra hop to search on phones. The `⌕` link does carry `aria-label="Search companies"`, so it's usable, just not inline. Low-ish, verify on a real device. |

---

## ⚪ Low / polish

| # | Issue | Where |
|---|-------|-------|
| L1 | Multi-word search is literal adjacent-phrase only (not token-AND): `q=health data` → 2 results, `q=data health` → different/none. Word order shouldn't decide matches. | `/companies?q=` |
| L2 | No min-length/word-boundary on search: `q=a` returns all 1,267; `q=AI` returns 808 (incl. coincidental substrings). Result counts aren't a relevance signal. | `/companies?q=` |
| L3 | "Discovered via **vc_portfolio**" shows the raw snake_case enum to customers (should be "VC portfolio"). | every company header |
| L4 | Bron's "Website" link points to `support.bron.org` (help center), not the homepage `bron.org`. | `/c/bron` |
| L5 | Funding rows with no date render a bare "—" for date (and sometimes round/amount), e.g. "— Graphite — — lead". | `/investor/shopify-ventures`, others |
| L6 | `/new` buckets rounds by *extraction* day, so old rounds (Jan 2024, May 2021, **2019**) appear under a "June 12, 2026" header with no cue. | `/new` |
| L7 | Several `/new` "Rounds" entries are a bare company name — no amount/stage/date (look like empty rows). | `/new` |
| L8 | Out-of-range numeric `?page=` hard-404s on tag pages (`/tag/embodied-ai?page=2`), unlike `/companies` which renders an (empty) page. | `/tag/*` |
| L9 | Mistyped tag 404 (`/tag/<x>`) renders **with no masthead/nav** — chrome-less dead-end (the global 404 also lacks branding, M6). | `/tag/*` |
| L10 | A 404 location page still emits a real-looking `<title>`/canonical/OG ("Startups in 99 — nous") because metadata runs before `notFound()`. | `/location/<bad>` |
| L11 | Homepage canonical is `…:3000` (no slash) while the sitemap lists `…:3000/` (with slash). | `/` vs `/sitemap.xml` |
| L12 | External company-site links open in a new tab with no "opens in new tab" cue (WCAG 3.2.5). | company headers |
| L13 | Primary `<nav>` has no `aria-label` and no `aria-current` for the active page (two unlabeled nav landmarks per page). | site-wide |

---

## What works well

- **No client-side errors.** Console is clean across home/companies/company pages (only dev-mode React-DevTools/HMR notices); **all 23 homepage network requests returned 200**; no hydration warnings despite the theme system.
- **Genuinely server-rendered & fast** — only `SpotlightDeck` ships client JS, so the site is light and the HTML is complete.
- **Search is injection-safe** — `' OR 1=1--`, `{{7*7}}`, `;DROP` etc. all return clean empty/normal results, never a 500 or SQL/template leak.
- **Theme toggle works and persists** across navigation; light and dark are both legible (money figures stay green); no flash-of-wrong-theme observed on reload.
- **Spotlight carousel works** (Fresha → Stilta, dot indicators track); **`/surprise` is genuinely random** (12/12 distinct targets, all 307, correctly excluded from sitemap and disallowed in robots).
- **Special-character tags round-trip correctly** — `/tag/ci%2Fcd`, `/tag/biotech-r%26d`, `/tag/fp%26a` etc. all 200.
- **No-fabrication discipline holds** — unknown values render "—" rather than guesses (the funding *gaps* are honest, even if disappointing).
- **Per-state pages** render real, correctly-paginated data; **company tag links** are sensible and resolve; **company/investor slug 404s** are branded and offer a way back.
- **Internal link integrity is otherwise excellent** — 1,498 of 1,506 crawled internal links returned 200.

---

## Coverage & caveats

**Tested:** homepage + global chrome; search (incl. injection/edge inputs); filters/sort/pagination + out-of-range; company pages (structure, degradation, markdown, badges); data trust (fabricated people, competitor notes, funding blanks, total-vs-rounds spot-check, date sanity); outbound website/news links; investors list + pagination; investor detail; location/state pages + case/edge; tag pages + encoding + sprawl; `/new`; `/surprise`; 404/routing; SEO/metadata/JSON-LD; full internal-link crawl (1,506 links); accessibility markup; and browser-only: console, network, theme persistence + no-flash, carousel, keyboard focus.

**Caveats / partial coverage:**
- **Mobile (<768px) was not visually rendered** — `resize_window` didn't shrink the viewport in this environment. The mobile-search collapse (G8) is confirmed from source, not a device. Recommend a real-device pass.
- **Account spend limit** killed the `link-crawl` finder and the `data-trust` verifier mid-run. I reproduced link-crawl by hand (found H9) and spot-checked data-trust by hand (total-vs-rounds consistent on Helion; no real future dates — a "2047" was a false positive inside a timestamp). A fuller data-trust re-verification (exhaustive attribution-completeness across many pages) was not re-run.
- Testing used live prod data that drifts during the 8×/day cron, so exact counts (1,267 vs 1,273 companies; 81 vs 85 investors) vary slightly between observations.
- The dev server (`http://localhost:3000`, background task) is still running.
