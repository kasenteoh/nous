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

2. **Momentum signals — the "open it every morning" hook.** — **SHIPPED (#181
   pipeline, #182 web).** `compute-momentum` scores every shown company's weekly
   acceleration (news recent-vs-baseline + funding recency + headcount growth,
   weight-renormalized to `momentum_score ∈ [0,1]`, NULL when insufficient data,
   $0, deterministic) into `companies.momentum_score`; `/trending` ("Heating up")
   ranks by it with a `🔥 Heating up` badge + a pipeline-worded "why" line. Scores
   populate on the weekly `discovery.yml` run once migration 0039 reaches prod;
   the page degrades to an empty-state until then. Follow-ups: homepage strip,
   badge calibration, per-industry scoping, a snapshot sparkline (BACKLOG).

3. **Per-entity RSS/feeds — alerts without accounts.** — **SHIPPED (#183).**
   Feeds scoped to an industry, an investor, or a single company — the existing
   `/feed.xml` pattern fanned out to `/c/[slug]/feed.xml`,
   `/industry/[group]/feed.xml`, `/investor/[slug]/feed.xml`. Power-user "watch
   this" that respects the no-accounts constraint and costs $0: shared row→item
   mappers with stable guids (`lib/rss-items.ts`), each feed empty-but-valid on
   missing data, `<link rel="alternate">` + a visible "Follow via RSS" link on
   every entity page. Follow-up (BACKLOG): a subscribe hint / feed hub; email
   delivery stays out this quarter.

4. **Talent-flow — "founder background" rider.** — **SHIPPED (#185 dry-run gate,
   #186 schema, #187 pipeline+golden, #188 web; #189 live recordings).** The #184
   probe found the rich "Stripe → founders → companies" *graph* isn't supported
   (named prior employers thin, ~13–18%, mostly non-catalog orgs), so the built
   bet is the per-company **"founder background / notable alumni" rider**: a
   bounded DeepSeek extraction of each founder's PRIOR employers →
   `career_moves`, rendered on `/c/[slug]` (linked when the prior employer is
   in-catalog). Evidence-gated husk-style — a $0.05 prod dry run cleared first
   (50% of top-funded yield ≥1 named prior, **0 fabrication**); empty-not-fabricate
   for the ~85% with no pedigree; golden-gated (grounding 1.0 live). **~$0.0013/
   company** measured (full backfill well under the ~$6.50 estimate). Follow-up
   (BACKLOG): a $0 low-precision "repeat founders" co-membership index.

5. **Investor depth.** — **SHIPPED** (co-investment pre-existing; portfolio
   momentum **#190**). Turned the investor directory from a list into a lens, $0
   / read-time, from existing linkage: "frequently co-invests with"
   (`getCoInvestors`) + a portfolio-momentum lens ("N of M portfolio companies
   heating up", reusing the #181 `momentum_score`) on `/investor/[slug]`.
   Follow-ups (BACKLOG): "who's leading rounds in industry X right now" (an
   industry-page surface) + a global co-investment meta-graph.

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
