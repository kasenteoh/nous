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

1. **Kill the husks by re-mining, not re-scraping.** Resolve the ~890 missing
   websites via, in order of preference:
   - **Outbound links in `news_articles`** we've already scraped (articles link
     the company site in-body) — zero new requests.
   - **VC portfolio pages** we already scrape into `raw_pages` (they link
     portfolio companies directly).
   - **Wikidata / Wikipedia** "official website" — free, un-Cloudflared API, and
     prominent companies are exactly who's indexed there.
   - **Common Crawl** — look a domain up in the index without hitting the origin.

   Ships as a new idempotent pipeline stage (e.g. `resolve-website-fallback`),
   $0, self-bounding on the husk population.

2. **A data-quality dashboard.** An internal report (backed by `pipeline_runs`
   and direct counts): % of companies with website / description / funding /
   logo / people, husk-count trend, duplicate rate, staleness distribution.
   *You can't fix what you don't measure* — this is the instrument panel for the
   entire horizon, and it makes every subsequent fix legible.

3. **Normalize the sloppy fields.** `hq_state` (CA ↔ California), `formatUsd`
   amount collapsing, tag hygiene / thin-tag merging. Cheap, compounding wins
   that raise perceived quality immediately.

4. **Re-enable "Report incorrect data."** The repo is public now, so the
   prefilled GitHub-issue link should resolve (it was disabled while private —
   `BACKLOG.md`). Turns on the human-in-the-loop correction signal. Highest
   trust-per-unit-effort item on the board.

5. **Per-company completeness / confidence score.** We already store sources and
   `extraction_confidence` per field — compute and expose (internally first) a
   completeness score so gaps are visible and prioritizable, and so #2 has a
   per-row primitive to aggregate.

---

## 🚀 Next — Turn clean data into depth pros return for

Built on top of the now-trustworthy foundation.

1. **The market map (`/map/[industry]`).** The designed-but-unbuilt payoff.
   `company_relationships` is already derived and clean — this is the visual,
   differentiating graph surface (competitor clusters, funding-weighted nodes)
   that nous is structurally able to show and nobody else bothers to.

2. **Momentum signals — the "open it every morning" hook.** `company_snapshots`
   already records weekly headcount + news velocity. Build detection on top:
   *accelerating* companies (hiring + news + funding cadence), "heating up this
   week." The single feature most likely to create a repeat power-user habit.

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
