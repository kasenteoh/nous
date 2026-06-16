# Product review & next steps — 2026-06-16

**Method.** Live production walkthrough (`nous-ksnxth.vercel.app`) driven through
Claude-in-Chrome, viewing the site as four personas in sequence: a **VC**, a
**CTO**, a **product manager**, and a **startup enthusiast**. Findings are
evidence-backed (every item below was observed on a live page) and
cross-referenced against [BACKLOG.md](../../../BACKLOG.md) and the
[2026-06-14 coverage-and-features plan](2026-06-14-nous-coverage-and-features.md).

**State snapshot (live).** 1,858 shown companies · 578 with funding data (~31%) ·
525 investor firms · "565 companies / 1,057 rounds in the last 7 days." DB well
under the 500 MB cap. No console errors on any page visited.

---

## What's shipped since the last plan (verified live — do not redo)

The 2026-06-14 plan's big lanes are effectively done in production:

- **Funding coverage** jumped from ~9.5% (06-14) to **~31%** of shown companies;
  rounds grew from ~288 to ~1,057-in-the-trailing-week. Lane A is done.
- **VC features (Lane C)** are all live: 6 sort modes (incl. largest raise /
  recently funded / headcount), advanced filters (stage, raised range, founded,
  headcount, source, recency), **watchlist**, **save search**, **CSV export**.
- **Taxonomy (Lane B / QA M1)** is normalized — the industry dropdown is a clean
  30-bucket canonical list, no freeform sprawl.
- **Relationship graph** (competitors, "similar", "also backed by") is live and
  the competitor analysis quality is high (e.g. Ramp → Brex with reasoning).
- **Status tracking** (Acquired / IPO / Shut down badges) works across cards and
  detail pages.
- **Footer disclaimer + "Report it"** issue link is live (repo is public now —
  the `repoIssueUrl` "unused until public" comment in `web/lib/site.ts` is stale
  and can be removed).
- **Observability** (`pipeline_runs`, `pipeline-health`) and the **Playwright
  smoke test** are in CI.

So the work below is the *next* frontier, not a re-run of the last plan.

---

## Findings by persona

### 👤 As a VC (deal flow, funding depth, credibility)

- **V1 — Marquee companies are blank husks. [P0]** Sorting by *Largest raise* —
  the first thing a VC does — surfaces **Perplexity, Mistral AI, Fivetran,
  Poolside, Hinge Health, ICEYE, Zepto, Abacus.ai, Affirm** etc. with **no
  description**. They have funding data (so they pass the catalog bar) but were
  never enriched. The single biggest credibility hit: the most important
  companies look empty. Root cause: `enrich-companies` works a generic queue at
  `--limit 30`/run and doesn't prioritise high-raise / high-news companies.
- **V2 — Non-US companies leak into a "US software startups" directory. [P0]**
  Among the largest raises: **Mistral AI (FR), ICEYE (FI), Zepto (IN), Clio
  (Canada — HQ shows "Burnaby"), Prodigy Finance (UK)**; in the new-this-week
  feed: **Rohlik (CZ), Bloom & Wild (UK)**. `infer-hq-country` (#109) targets
  exactly this but hasn't drained the backlog, and famous foreign companies enter
  via funding news faster than they're judged.
- **V3 — Funding history lacks dates and round types. [P1]** On Perplexity every
  row shows `—` for date *and* round type; only amount + valuation are present.
  A VC can't read the trajectory ("when did the Series C happen?"). Also visible:
  **duplicate / empty rounds** (two blank rows that only carry a `$20B`
  valuation) that `repair-duplicate-rounds` should collapse.
- **V4 — Investor pages are thin. [P1]** `/investor/[slug]` shows only "Backs N
  companies · Led N rounds" + a portfolio grid. No firm description, **no link to
  the firm's website** (QA G4/G5, still open). The co-invest signal exists but is
  buried below a very long portfolio.

### 👤 As a CTO (correctness, data integrity, technical depth)

- **C1 — Wrong-website → mismatched descriptions in prod. [P0]** **Kalshi's**
  card carries **FrenFlow's** description ("multi-venue prediction-market
  platform… copy-trade across Polymarket, Kalshi…"); **AgentMail's** card carries
  **"Series V"**'s description. Homepage resolution accepted the wrong site and
  enrichment described the wrong company. `repair-wrong-websites` exists but isn't
  catching these — they're live. Needs a "description name ≠ company name"
  detector that re-queues the row.
- **C2 — Husk notice contradicts the data on the same page + copy bug. [P0,
  quick]** Perplexity shows "$12.4B raised · $41B valuation" plus a 6-round
  history and ~35 news items, *and* the notice **"We've discovered Perplexity via
  vc portfoliobut haven't built a full profile yet."** Two bugs: (a) the notice
  should be suppressed when funding/news/competitor data exists; (b) literal
  string "**portfoliobut**" (missing space) and the raw `vc_portfolio` enum
  instead of "a VC portfolio". `web/app/c/[slug]/page.tsx`.
- **C3 — News mis-attribution. [P1]** Perplexity's News list contains
  *"Autoscience Raises $14M…"* — a different company's funding article. The
  broad-sweep / per-company news matcher is occasionally attaching articles to
  the wrong company.
- **C4 — Sources section collapses to `news.google.com`. [P1]** Every funding
  source on the Perplexity page reads `news.google.com` even though the News
  section resolves real publishers (Reuters, Bloomberg, TechCrunch…). The funding
  rows store the Google-News redirect URL as `primary_news_url`; resolve it to the
  publisher before storing so Sources are useful (related to the existing
  "description-source attribution misleading" backlog item).
- **C5 — Non-startup entities pass eligibility. [P1]** **Manta** ("online
  business directory… operating for over 20 years") and **Lucra** ("courses,
  coaching… mindset mastery") are surfaced as startups on the home page.
  `judge-eligibility` should catch "not a startup"; tighten the prompt / re-judge.
- **C6 — `discovered_via` badge shows the raw enum. [P2, quick]** Detail pages
  render "Discovered via **vc_portfolio**" (underscore) instead of "VC portfolio".
  The `SOURCE_LABELS` map used on `/companies` isn't applied on `/c/[slug]`.

### 👤 As a product manager (UX, IA, discoverability)

- **P1 — Compare is built but inaccessible. [P1]** `/compare` works, but its
  empty state instructs the user to **hand-type a URL**: "Add 2–4 companies via
  `/compare?slugs=acme,globex`." There is no add/select UI, no "compare"
  control on cards or the browse page. A real user can't reach the feature. Add a
  selection affordance (checkbox on cards → "Compare (n)" bar, or reuse the
  watchlist store).
- **P2 — Investor portfolio pages have no pagination. [P1]** a16z renders all
  **678** portfolio cards on one page (~100 KB of text). Heavy DOM, long scroll,
  and it pushes the co-investor module far down. Paginate or cap + "see all".
- **P3 — Mobile responsiveness unverified. [P2]** Could not confirm the mobile
  layout through the review harness (capture stayed at desktop width). Worth a
  manual phone-width pass — the home page is a 2-column spotlight+aside layout
  and the nav has no visible hamburger.
- **P4 — "Largest raise" mixes in exited/foreign companies. [P2]** The default
  largest-raise view leads with blank/foreign/IPO'd/shut-down companies. Consider
  an "active US only" default or a quick toggle.

### 👤 As a startup enthusiast (browsability, freshness, fun)

- **E1 — The core experience is genuinely good.** Spotlight, "trending", "new
  this week", and rich profiles (Ramp) are engaging; the terminal aesthetic is
  distinctive; "Surprise me" + watchlist add play. This persona is well served —
  the gaps are data quality (above), not engagement.
- **E2 — News lists are noisy. [P2]** A funded company shows 20+ near-duplicate
  articles about the *same* round (plus low-quality aggregators: Tracxn,
  CryptoRank, SQ Magazine…). De-dupe/rank news by round and source quality.
- **E3 — Logos are missing. [P2]** Cards and headers are text-only.
  `companies.logo_url` exists but is unused (already a Wave-1 backlog item) —
  favicons would make browsing far more scannable.

---

## Prioritized next steps

Ordered by impact on the core promise (credible US-software-startup discovery).
"**New**" = not in BACKLOG.md; "**listed**" = already tracked (status updated).

### P0 — credibility-critical (the site currently looks wrong)

1. **Suppress the husk notice when data exists + fix the "portfoliobut" copy /
   enum.** (C2) Pure frontend, ~1 hr, highest visible payoff. **New.**
2. **Prioritise enrichment of high-value companies.** (V1) Order
   `enrich-companies` by `latest_round_amount` / news volume so flagship
   companies aren't blank; consider a one-off drain of the funded-but-husk set.
   **New.**
3. **Drain & strengthen non-US detection.** (V2) Run `infer-hq-country` to
   completion over the backlog (Actions dispatch); add a known-foreign-company /
   ccTLD heuristic so Mistral-class names are caught on entry. **Listed**
   (extends #109).
4. **Detect & repair wrong-website / mismatched descriptions.** (C1) Add a guard
   that flags rows where the description's named company ≠ the row's name and
   re-queues resolve→scrape→enrich. **New** (extends `repair-wrong-websites`).

### P1 — quality & trust

5. **Tighten eligibility for non-startups.** (C5) Re-judge; the prompt should
   reject directories/coaching/info-product businesses. **Listed** (QA M4-adjacent).
6. **Fix funding-history dates/types + collapse duplicate/empty rounds.** (V3)
   Improve `announced_date`/`round_type` extraction; run `repair-duplicate-rounds`
   over the new backlog (it's currently dry-run by default). **Listed/refine.**
7. **Add a Compare selection UI.** (P1) Checkbox on cards → sticky "Compare (n)"
   bar building the `?slugs=` URL; reuse the watchlist store. **Listed** (Wave 4
   "Compare view" — page exists, entry UI missing).
8. **Make the Sources section show real publishers.** (C4) Resolve the
   Google-News redirect to the publisher before storing `primary_news_url`.
   **New/refine.**
9. **Fix news mis-attribution.** (C3) Tighten the article→company match in the
   broad sweep. **New.**
10. **Enrich investor pages + paginate portfolios.** (V4, P2) Populate/render
    `investors.description` + `website`; paginate `/investor/[slug]` portfolio.
    **New** (QA G4/G5).

### P2 — polish & breadth

11. **Humanise the `discovered_via` badge** on `/c/[slug]`. (C6) ~15 min. **New.**
12. **Investor dedup gaps:** merge **a16z** into **Andreessen Horowitz** (alias),
    drop junk firms ("a group of investors"), mark individuals (Jeff Bezos) as
    angels not firms. **New** (extends `dedup-investors` + QA M9).
13. **De-dupe/rank company news** by round + source quality. (E2) **New.**
14. **Logos via favicon fetch.** (E3) **Listed** (Wave 1).
15. **Verify mobile responsiveness** at phone widths. (P3) **New.**

### Quick wins to bundle first (all frontend, low risk)

C2 (husk notice + copy), C6 (badge label), the stale `repoIssueUrl` comment, and
the Compare entry UI (P1) are small, safe, and immediately visible — a good first
PR before the data/pipeline work.

---

## Still-open items from prior QA that this review re-confirmed

- G2 thin profiles for major companies → **V1** above.
- G4/G5 investor descriptions + links → **V4** above.
- M9 individuals shown as investor firms → step 12 above.
- "Description-source attribution misleading" (BACKLOG) → **C4** above.

Everything else in BACKLOG.md (embeddings/semantic search, themes, trends,
market map, digest, tech-stack chips, discovery adapters, AI-answer distribution)
remains valid future work and is unchanged by this review.
