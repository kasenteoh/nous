# Prompt: Customer-perspective QA of nous (Claude in Chrome)

> Paste everything below the line into a fresh Claude Code session that has the
> **Claude in Chrome** browser extension connected. It is fully self-contained.

---

You are a meticulous QA tester and first-time visitor to **nous**, a public website
for discovering US software startups (company profiles assembled from VC portfolios,
funding news, and LLM-written summaries). Your job is to **use the site like a real
customer through a real browser and find every bug, rough edge, content problem, and
missing-but-expected feature you can.** Produce a thorough, prioritized findings report.

## Mission

Discover, from the customer's point of view, everything that is:
- **Broken** — errors, crashes, broken links, wrong/garbled data, visual defects, layout
  breakage, console/network errors, things that don't work.
- **Confusing or low-quality** — bad copy, untrustworthy or contradictory data, dead-end
  pages, poor empty states, confusing flows.
- **Missing** — features a visitor would reasonably expect on a startup-discovery site
  that aren't there ("anything else that should be included").

Be exhaustive. Breadth first (touch every page and control), then depth (push each one
until it breaks or proves solid). Assume nothing works until you've seen it work.

## Hard rules

1. **No database access.** Never query, open, or inspect Supabase/Postgres or any internal
   data store. Experience the site only through the browser, exactly as a customer would.
2. **Customer lens.** Judge everything as a non-technical visitor would: "Is this clear?
   Does it work? Would I trust this number? Can I find what I want?"
3. **Read-only.** Do **not** edit application code, run the data pipeline, change git
   branches, or commit anything. The only files you create are your report and screenshots.
4. **Confirming root cause is allowed, but optional and secondary.** You *may* read source
   files (`web/app`, `web/components`, `web/lib`) **only** to confirm a bug you already
   observed in the browser and make the finding actionable. Always lead with the observable
   customer symptom; mark any code-derived note clearly as "suspected cause." Never let
   source-reading replace actually exercising the UI.
5. **Don't submit anything that writes to the outside world** (e.g. if you find a "report an
   issue" link that opens a GitHub issue form, inspect it but do not submit it). Don't hammer
   external sites.

## Step 0 — Get the site running

The app is a Next.js app in `web/` that reads live production data from Supabase.

```sh
cd web
npm install        # only if node_modules looks missing/stale; usually already installed
npm run dev        # serves http://localhost:3000 — start it and wait for "Ready"
```

Run the dev server in the background, wait until it's listening on
**http://localhost:3000**, then drive that URL in Chrome. This shows **real customer data**
(real companies, real LLM-written copy), which is exactly what you want for content-quality
findings.

**Gotchas to recognize, not misreport:**
- If a **production URL** is provided to you, prefer testing that instead (note which target
  you used). Otherwise use local `npm run dev`.
- The Supabase free-tier project can **auto-pause when idle** and fail fast. If *every* page
  is uniformly empty ("No companies indexed yet", empty investor list, `/surprise` bouncing
  to an empty `/companies`) and the dev-server logs show DB errors like "tenant not found,"
  the database is likely **asleep, not broken** — note it as an environment caveat and don't
  file the whole site as a critical bug. Real findings need real data on the page.
- Pages are cached/ISR (~6h). That's expected; don't report stale-after-edit as a bug.

## Tooling (Claude in Chrome)

Use the `mcp__Claude_in_Chrome__*` tools to drive a real browser:
- `navigate`, `read_page` / `get_page_text`, `find` — load and read pages.
- `computer` (click/scroll/type), `form_input`, `file_upload` — interact.
- `read_console_messages` — **check on every page** for errors/warnings/hydration issues.
- `read_network_requests` — catch failed requests, 4xx/5xx, broken assets.
- `resize_window` — test responsive breakpoints (see below).
- `screenshot` / `gif_creator` — capture evidence for every finding.

Check the browser console and network panel on **every** page you visit — many bugs are
invisible visually but loud in the console.

## Methodology (repeat per page)

For each route/control:
1. **Observe** — load it, screenshot it, read console + network.
2. **Interact** — click every link/button, submit every form, change every filter/sort,
   page through results, follow outbound links.
3. **Stress it** — feed edge-case inputs and URLs (see "Edge cases" below).
4. **Judge as a customer** — clarity, trust, usefulness, dead ends.
5. **Log** — anything off, with evidence, expected-vs-actual, severity, and customer impact.

First do a discovery pass purely as a lost customer ("can I even find everything from the
homepage and nav?") and note anything **not reachable through the UI**. Then use the
coverage map below to guarantee nothing is skipped.

## Coverage map — every route and feature

Test all of these. The site map is intentionally given so you don't miss anything; still
note which of these a normal customer could *not* have discovered on their own.

**Routes**
- `/` — homepage: spotlight deck (rotating/featured companies), margin notes ("New this
  week" count → `/new`, "Recent fundings", "New on nous"), bottom row of top industries +
  "Browse all N →". Empty-state copy when nothing to spotlight.
- `/companies` — browse/search/filter/sort + pagination. Controls: text search `q`,
  **Industry** dropdown, **Source** dropdown (VC portfolio / News / TechCrunch / Unknown),
  **Sort** (Name A–Z, Name Z–A, Recently added), Apply, Clear, Prev/Next, "Showing X–Y of N".
- `/c/[slug]` — company profile. Verify each section renders correctly *and* degrades
  gracefully when data is absent: header meta (website link, location→state link, "Est."
  year, industry, employee range, "Profile updated" date, "Possibly inactive" rider),
  **status badge** (Acquired/Shut down/IPO) + **"Discovered via"** badge, **Total raised**
  key-fact with inline source attribution, **About** (Markdown), **primary category + tags**
  (tags link to `/tag/…`), **Team/leadership**, **Funding history**, **Investors** (names
  link to investor pages), **Competitors**, **Related companies** (similar + "also backed
  by"), **News** (external article links).
- `/investors` — firms ranked by portfolio size, paginated (50/page).
- `/investor/[slug]` — one investor: portfolio (company cards) + recent funding activity.
- `/location/[state]` — companies HQ'd in a state (sortable, paginated).
- `/tag/[tag]` — companies with a tag (sortable, paginated).
- `/new` — "New this week" feed, bucketed by day.
- `/about` — static explainer (discovery, sourcing, update cadence, caveats).
- `/surprise` — redirects to a random company; should give a *different* one most visits and
  never 500 on an empty index.
- `/sitemap.xml`, `/robots.txt`, and OpenGraph images (`/opengraph-image`, and a company's
  OG image) — load them, confirm they're valid and not erroring.

**Site-wide chrome**
- Masthead: "nous" wordmark (→ home), **search box** (note: it's only shown on wider screens;
  on narrow screens it collapses to a `⌕` link to `/companies` — verify mobile search is
  actually usable), nav (Browse / Investors / Surprise me / About), **theme toggle**.
- Footer (is there one? note if missing).
- 404 / not-found pages for bad company and investor slugs, and for unknown routes.

## Cross-cutting dimensions (apply across the whole site)

- **Responsive:** test at mobile (~375px), tablet (~768px), and desktop (~1280px). Watch the
  masthead (does search disappear? is the collapsed search usable?), the company grid columns,
  long names/overflow, tap targets.
- **Theme:** dark (Tokyo Night) is the default. Toggle to light and back. Check: no flash of
  wrong theme on reload, choice persists across navigation, and **every** page is legible in
  **both** themes (watch muted text, the green "money" figures, accent links, borders).
- **Accessibility:** keyboard-only navigation (Tab/Enter through masthead, filters, pagination,
  links), visible focus states, heading hierarchy, form labels, image alt text, color contrast,
  presence/absence of a skip-to-content link.
- **Console & network:** zero tolerance scan on every page — JS errors, React hydration
  warnings, failed/4xx/5xx requests, broken images.
- **SEO/meta:** per-page `<title>` and meta description, canonical URLs, OG image + tags,
  JSON-LD structured data on home and company pages.
- **Performance/UX feel:** obvious slowness, layout shift, missing loading states.
- **Content quality & trust** (this is a data product — scrutinize hard):
  - Does every rendered number have a **visible source**? (The site promises "every fact
    traces to a source" — look for unattributed figures.)
  - Do funding figures look sane? Does the headline **"Total raised"** ever **contradict or
    exceed** the sum of the funding rounds listed below it?
  - LLM-written descriptions: truncated, generic, obviously wrong, mis-formatted Markdown,
    or fabricated-looking?
  - Outbound **company website** links: do they resolve? Any landing on **parked / "domain
    for sale" / dead** pages while nous presents them as a real company? (Credibility bug.)
  - Competitors and "related companies": actually relevant, or noise?
  - News items: real, on-topic, and not stale-presented-as-fresh? Links work?
  - Dates: any in the future or implausible? "Est. <year>" sane?

## Edge cases & URL fuzzing (try these explicitly)

- Search: empty/whitespace `q`, a term with **no results**, mixed case, multi-word, a very
  long string, punctuation/quotes/`%`/`<>` and SQL-ish input (e.g. `' OR 1=1--`) — should
  return clean "no results," never an error or odd behavior.
- Filters combined: `q` + industry + source together; filtering by **Source = Unknown**;
  pick an industry then change sort — do active filters **persist** across pagination and
  the Clear link?
- Pagination params on `/companies`, `/investors`, `/location/[state]`, `/tag/[tag]`:
  `?page=0`, `?page=-1`, `?page=99999` (past the last page), `?page=abc`, `?sort=garbage`.
  Confirm graceful handling and that Prev/Next disable correctly at the ends and the
  "Showing X–Y of N" math is right.
- Bad/edge slugs: `/c/does-not-exist`, `/investor/does-not-exist` (→ proper 404s),
  trailing slashes, and **special characters / spaces** in `/location/<state>` and
  `/tag/<tag>` (e.g. a multi-word state, or a tag containing `&`, `/`, or a space) — verify
  encoding round-trips and doesn't 500.
- `/surprise` several times in a row — confirm it varies.
- Old/odd query strings on `/` (e.g. `/?q=foo`) — should just render the homepage.
- Investor links on a company page: are **some** investor names linked while others are
  plain text (no page)? Any investor page with an **empty** portfolio?

## Specific things to probe (verify — do not assume)

These are suspected weak spots from a first look. Confirm each by hand in the browser:
1. **Mobile search** is hidden behind a `⌕` link to `/companies` — is searching actually
   doable and discoverable on a phone?
2. **No feedback / "report incorrect data" path** for customers — the site stakes its
   reputation on accuracy but may offer no way to flag a wrong fact or contact anyone. Confirm
   whether any such affordance exists anywhere (company page, About, footer).
3. **"Total raised" vs. itemized rounds** — find a company with both a stated total and listed
   rounds; check the headline number isn't confusingly larger/different than the visible rounds.
4. **Sparse companies** — find profiles missing description / funding / team / competitors /
   news and confirm they degrade gracefully (no empty section shells, stray "—", or broken look).
5. **Parked / dead company websites** surfaced as real companies (content-trust bug).
6. **Empty states everywhere** — `/new` on a quiet week, `/companies` with no matches, an
   investor with no rounds, the homepage with nothing to spotlight: are they all graceful?

## Evidence & severity

For every finding capture: the **URL**, **exact steps**, a **screenshot** (and console/network
excerpt if relevant), **expected vs. actual**, the **customer impact**, and a **severity**:
- **Critical** — broken core flow, crash, data that destroys trust, security-smelling behavior.
- **High** — feature broken or clearly wrong, significant confusion, broken on mobile.
- **Medium** — noticeable rough edge, minor data/UX issue, accessibility gap.
- **Low** — polish, cosmetic, nice-to-have.
- **Gap** — missing feature/affordance a visitor would expect (tag separately).

De-duplicate before finalizing. Don't pad the report with non-issues; if something works
well, a short "verified working" list is fine, but the focus is problems and gaps.

## Deliverable

Write a single Markdown report to **`docs/qa/findings-customer-perspective.md`** and save
screenshots under **`docs/qa/screenshots/`**. Structure it:
1. **Test target & environment** — local vs prod URL, date, whether data looked fully
   populated (or DB-asleep caveat), browser.
2. **Executive summary** — the top 5–10 issues, highest impact first.
3. **Findings** — grouped by severity, each with the evidence fields above. Reference
   routes/files as clickable paths where useful.
4. **Gaps / missing features** — "anything else that should be included," prioritized.
5. **Coverage** — every route/dimension you tested (and anything you couldn't, and why).

Do **not** commit, change branches, or edit app code. End by telling me the report path and
giving me the headline: how many findings by severity, and the single most important thing to
fix first.
