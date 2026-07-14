# Roadmap

> **Living document.** This is the strategic layer that sits *above*
> [`BACKLOG.md`](BACKLOG.md). The roadmap answers *why* and *in what order*;
> the backlog is the tactical grind queue that answers *what next*. When a
> roadmap bet becomes concrete work, it drops into the backlog as P0–P2 items.
> Started 2026-07-13.

## How this doc works

- **Horizons, not dates.** nous is a small, autonomous operation — a Gantt
  chart would be fiction. Work lives in **Now / Next / Later** by conviction and
  dependency order, not calendar promises.
- **Update it when the strategy moves,** not on every PR. Move items between
  horizons as they ship or get reprioritised; annotate shipped bets with the PR
  number and delete them once they're fully absorbed into the product.
- **The backlog is downstream.** A roadmap item is a *bet*; a backlog item is a
  *task*. Don't put task-level detail here.

## North star

**Data quality first, then depth.** You cannot earn trust on a foundation of
husk companies and unnormalized fields, and depth features (the market map, the
relationship graph, momentum signals) are far better built *on top of* clean
data than *under* it. So the sequence is deliberate: measure and fix the
foundation (**Now**) → build depth pros return for (**Next**) → make the trust
visible as a feature (**Later**).

## The moat, and the constraint it imposes

The moat is **trustworthy, fully-sourced data**: every rendered fact has a
recorded source, and the model leaves fields blank rather than guess (see
`CLAUDE.md` "Non-negotiable rules"). This is not just a policy — it *constrains
the roadmap*. No unsourced narrative. No hallucinated numbers. No feature that
trades correctness for coverage.

### Guiding principle: route around, don't evade

The biggest data-quality liability is the **~890 "husk" companies** with no
resolvable website, because scrapes get Cloudflare-403'd from datacenter
(GitHub Actions) IPs. The tempting fix — proxy evasion, account farming, cat-and-
mouse with Cloudflare — is **rejected on principle**, for three reasons:

1. **It contradicts the moat.** Our scraping etiquette (contact-email
   User-Agent, `robots.txt`, 1 req/sec — `CLAUDE.md`) is the *same* trust brand
   as "every fact is sourced." Adversarial evasion is off-brand and risks the
   contact identity getting flagged.
2. **It rots.** Evasion breaks the week Cloudflare updates. Anything load-bearing
   built on it is a latent outage.
3. **It's unnecessary.** Husk companies are *prominent* — that's exactly the
   population whose website exists in a dozen places that aren't their
   Cloudflare-protected homepage.

So the rule is: **resolve missing data from sources that were never the origin
site** — mostly by re-mining data we already paid to fetch. Same audacity,
on-brand, doesn't rot.

---

## 🔨 Now — Earn the right to be trusted

The data foundation. Priority horizon. Measure quality → fix the biggest hole
cleverly → make correctness visible.

1. **Kill the husks by re-mining, not re-scraping.** — **SHIPPED (#172/#173/#174).**
   The `resolve-website-fallback` stage resolves website-less husks from
   non-origin sources ($0, idempotent, in the 3h cron, provenance recorded):
   **Wikidata "official website"** (P856, name + org-type + country matched) and
   **outbound links in already-sourced news article bodies** (re-fetching the
   article, not the origin). A 30-husk prod dry run resolved 37% at ~10/11
   precision, 0 conflicts. The two sources the roadmap first named that weren't
   built: VC-portfolio (`raw_pages` is company-scoped, not portfolio pages, and
   the portfolio adapters already capture the URL at discovery — redundant) and
   Common Crawl (weak for name→domain); revisit only if the husk count stays
   high. The re-mining principle held: no origin fetch, no evasion, every
   resolved site sourced.

2. **A data-quality dashboard.** — **SHIPPED (#175).** Read-only `data-quality`
   stage emits a step-summary completeness report (field %s, website provenance
   by source, completeness-score distribution, duplicate rate, staleness) — the
   instrument panel that makes every subsequent fix legible. A web-facing
   version is deferred to Later (provenance UI).

3. **Normalize the sloppy fields.** — **SHIPPED (#176/#177).** `hq_state`
   canonicalized to the USPS code at enrichment + a `normalize-hq-state` backfill
   (routing-safe — heals broken `/location/California` links); `formatUsd` exact-
   dollars tooltips; thin single-company tag pages `noindex`'d in lockstep with
   the sitemap's existing ≥3 filter.

4. **Re-enable "Report incorrect data."** — **SHIPPED (#177).** Per-company
   `repoIssueUrl` rider restored on `/c/[slug]` (repo public → the prefilled
   issue link resolves). The human-in-the-loop correction signal is live.

5. **Per-company completeness / confidence score.** — **internal primitive
   SHIPPED (#175)** (`util.completeness`, aggregated by the dashboard). Remaining:
   wire it into husk-enrichment ordering, fold in `extraction_confidence`, and
   expose a public trust badge (Later — provenance UI).

---

## 🚀 Next — Turn clean data into depth pros return for

Built on top of the now-trustworthy foundation.

1. **The market map (`/map/[industry]`).** — **SHIPPED (#179 pipeline, #180
   web).** A pipeline PCA(2) projection of company embeddings → per-industry 2D
   coords (`compute-map-positions`, deterministic, TTL-gated, $0) rendered as a
   static server SVG at `/map/[industry]` (funding-sized nodes, canonical-gated,
   ML kept off the web function per #157). Coords fill on the discovery cadence.
   Follow-ups: interactive client renderer + theme coloring + a global
   theme-level meta-graph (BACKLOG).

2. **Momentum signals — the "open it every morning" hook.** `company_snapshots`
   already records weekly headcount + news velocity. Build detection on top:
   *accelerating* companies (hiring + news + funding cadence), "heating up this
   week." The single feature most likely to create a repeat power-user habit.
   **Web surface landed (branch `fable5/momentum-web`):** `/trending` ("Heating
   up") ranks by a `companies.momentum_score` the web only reads, with a
   `🔥 Heating up` badge (threshold `MOMENTUM_BADGE_THRESHOLD = 0.65`), a
   pipeline-authored "why" line, nav + footer + sitemap links; it degrades to an
   empty state until the scorer ships. **Still pending:** the pipeline half —
   migration 0039 (`momentum_score` + index, `momentum_computed_at`,
   `momentum_why text[]`) and the detection stage that computes the score. The
   page and badge light up automatically on the next revalidate once scores land.

3. **Per-entity RSS/feeds — alerts without accounts.** Feeds scoped to an
   industry, an investor, or a single company — the existing `/feed.xml` pattern
   fanned out. Power-user "watch this" that respects the no-accounts constraint
   and costs $0.

4. **Talent-flow graph.** `people` already ties founders/leaders to companies.
   Surface "founder previously at Stripe/Google," repeat founders, exec moves —
   the human network VCs actually care about.

5. **Investor depth.** Co-investment networks, portfolio momentum, "who's leading
   rounds in X right now" — turn the investor directory from a list into a lens.

---

## 🔭 Later — Make the moat visible, and speculative bets

Depends on the foundation being solid, so genuinely later.

1. **Provenance UI, made public.** Inline sources, "last verified,"
   completeness/confidence badges on the profile — turn "we don't hallucinate"
   from a claim into a *visible feature*. This is where data quality becomes a
   distribution surface.

2. **Sharpen AI-answer surfaces.** `llms.txt`, `/c/[slug].md`. As answer engines
   increasingly cite sources, "fully-sourced" is a structural advantage worth
   leaning into.

3. **Speculative wedges (parked, not committed):**
   - Embeddings-powered "find companies like this" as a first-class surface.
   - A lightweight public JSON API (deferred today for egress/abuse — revisit
     once quality justifies the exposure; `BACKLOG.md`).
   - Themed digest feeds.

---

## 🩺 Cross-cutting — Platform health

Not features — load-bearing debt that will otherwise cap every horizon above.

- **Decouple the embedding model from the `/companies` Vercel function.** It's
  one dependency bump from the 250MB limit; currently held together by
  `next build --webpack` + `VERCEL_SUPPORT_LARGE_FUNCTIONS=1`. A latent outage.
- **Refactor `pipeline.yml` off the 25-input cap** (config-driven single input)
  before the next stage needs an input slot that doesn't exist.
- **Observability.** Sentry free tier + surface `pipeline_runs` health, so silent
  pipeline degradation becomes visible rather than discovered by a stale page.

---

## Deliberately *not* doing (and why)

Carried forward from `BACKLOG.md` — recorded so they don't get re-litigated:

- **User accounts / auth** — no-login is a product stance; watchlists stay
  browser-local.
- **LLM-written narrative reports** — one hallucinated claim damages the trust
  that is the moat. Deferred indefinitely.
- **Email digests** — the first true recurring cost item; deferred until there's
  a reason worth paying for.
- **Proxy/evasion scraping** — rejected on principle (see "route around, don't
  evade" above).
