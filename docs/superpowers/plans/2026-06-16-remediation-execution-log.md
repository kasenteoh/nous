# Remediation execution log — 2026-06-16

Execution record for the "address the bugs + backlog" drive that followed the
[2026-06-16 product review](2026-06-16-product-review-and-next-steps.md). All
work shipped as small, reviewed, CI-green PRs merged to `main`, then verified on
production. Parallel agents in isolated worktrees implemented each fix with
tests; every diff was reviewed before merge and CI gated every merge.

## What shipped (18 PRs)

| PR | Area | Change |
|----|------|--------|
| #112 | web | Suppress husk notice on data-rich pages; humanize `discovered_via` badge |
| #113 | pipeline | Investor dedup: merge `a16z`→Andreessen, purge junk investors, classify angels |
| #114 | pipeline | Prioritise the resolve→scrape→enrich funnel by funding size (marquee husks) |
| #115 | pipeline | Eligibility rejects non-software-startups (Manta/Lucra) + opt-in re-judge path |
| #116 | pipeline | News relevance guard — drop mis-attributed articles for generic names (Aardvark) |
| #117 | pipeline | Harden homepage resolution + repair wrong-company profiles (Kalshi→FrenFlow) |
| #118 | pipeline | Store publisher URLs as funding source, not `news.google.com` redirects |
| #119 | web | Compare selection UI (card toggle + sticky bar) |
| #120 | web | Paginate investor portfolios; richer investor header (type/description/website) |
| #121 | web | Exact-amount tooltips + honest description attribution |
| #122 | pipeline | Extract company logos (favicon) into `logo_url` |
| #123 | pipeline | `name-quality` casing stage + `judge-eligibility --rejudge-nonstartup-signals` |
| #124 | pipeline | Collapse phantom valuation-only funding rows (preserves the #107 invariant) |
| #125 | web | Render logos (monogram fallback) + normalize US state display |
| #126 | web | "Alternatives to X" pages, FAQ JSON-LD, card-logo plumbing |
| #127 | pipeline | `adapter-health` canary for VC scrapers |
| #128 | db | Index `hq_state`, `tags` (GIN), `industry_group`, `discovered_via` (migration 0030) |

Plus #110 (About+README accuracy) and #111 (the review doc) earlier in the day.

**Verified live on prod:** husk fix, humanized badges, compare flow (toggle →
sticky bar → comparison, persists across pages), Alternatives pages, card +
header logos (monogram fallback until favicons backfill), investor pagination.
No console errors on any page checked.

## Activation — what heals automatically vs. what to dispatch

The code is merged; the data catches up two ways.

### Heals automatically (no action — the cron does it)
- **`pipeline.yml`** (every 3h) applies **migration 0030** (`alembic upgrade head`),
  then on each run: enrichment now works the **highest-raise companies first**
  (marquee husks fill in), `judge-eligibility` **rejects new non-startups**,
  `ingest-news` **drops mis-attributed articles**, `extract-funding` stores
  **publisher URLs**, and `scrape-homepages` **populates logos**.
- **`discovery.yml`** (weekly, Mon 02:00 UTC) runs `dedup-investors`, so the
  **a16z↔Andreessen merge**, junk-investor purge, and angel classification land
  on the next weekly run (or dispatch it to apply sooner).

### One-time cleanups to dispatch on return (deliberately NOT auto-run)
These mutate existing rows. They are reviewed high-precision and recoverable,
but I did not trigger prod data mutations unattended. Run them via GitHub
Actions (prod DB ops are Actions-only):

1. **Wrong-company profiles** (Kalshi→FrenFlow, AgentMail→Series V): dispatch
   `pipeline.yml` with **`run_repair_websites=true`** — runs `repair-wrong-websites`
   including the new high-precision pass (e). Cleared rows re-resolve/re-enrich
   on the next cron.
2. **Phantom valuation rounds** (Perplexity's blank `$20B` rows): dispatch
   `pipeline.yml` with **`run_repair_dupes=true`** (ensure non-dry-run) — runs the
   new phantom-collapse pass.
3. **Leaked non-startups** (Manta, Lucra) under the tightened prompt: run
   `judge-eligibility --rejudge-nonstartup-signals`. The flag exists on the CLI
   but is not yet a `workflow_dispatch` input — add it as an input (or a one-off
   step) to run in prod.
4. **`name-quality`** (casing, e.g. Docusign→DocuSign) and **`adapter-health`**
   (scraper canary): new CLI commands, not yet wired into a workflow. Add them as
   steps in `discovery.yml` (both are cheap; name-quality is case-only-safe,
   adapter-health is read-only `--strict` for alerting).

A small follow-up PR can wire 3/4 into the workflows as default-off dispatch
inputs — I held off editing the cron YAML unattended to avoid risking the
every-3h schedule while away.

## Deferred (large / speculative / needs product direction)
Left for deliberate, reviewed work — building these unattended would be the
opposite of responsible:
- **Wave-3 intelligence:** embeddings infra → semantic search → themes pipeline →
  `/industry/[group]` → `/trends` dashboard → similar-companies. A connected bet
  on direction; embeddings is the gateway and deserves a deliberate rollout.
- **Market-map visualization** (`/map/[industry]`, the first client component).
- **Weekly digest + RSS**, **`company_events` timeline** (product bets).
- **`slug_aliases` / `company_aliases`** (dedup-merge 301s; migration + middleware).
- **"X vs Y" SEO pages**, **`llms.txt` + `/c/[slug].md`** (next SEO tranche — the
  Alternatives pages + FAQ JSON-LD shipped this round are the start).
- **`ThrottledHTTPClient`** refactor; **prompt versioning** (needs a column);
  **missing-env fast-fail** (must not break the env-less CI build — needs care);
  small extras (startup-of-day, funding-timeline SVG, tech-stack chips, more
  discovery adapters).
- Explicitly deferred in BACKLOG: accounts/auth, public API, email digest.
