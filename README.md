# nous

nous is a free, public-facing site for discovering and reading about US software
startups. It aggregates VC portfolio listings, funding news, and public web
sources, then uses LLM enrichment to turn them into readable, source-attributed
company pages. A scheduled pipeline keeps the data fresh on its own — funding
news every few hours, full discovery and analysis weekly.

See [nous-technical-spec.md](nous-technical-spec.md) for the full product and
technical design, and [CLAUDE.md](CLAUDE.md) for working conventions in this repo.

## What the site offers

- **Readable company pages** — an LLM-written short and long description, key
  facts (total raised, latest valuation), leadership, full funding history with
  sourced rounds and investors, competitors, related and co-invested companies,
  recent news, and a consolidated list of every source cited on the page.
- **Browse, search & filter** — full-text search plus filters for industry,
  funding stage, total raised, founding year, headcount, recency, and discovery
  source; sort by name, recency, raise size, most-recently-funded, or headcount.
- **Investor directory** — every VC and investor ranked by portfolio size, with
  round history and "frequently co-invests with" signals.
- **Power-user tools** — side-by-side compare of 2–4 companies, a browser-local
  watchlist and saved searches (no account needed), and CSV export of any
  filtered view.
- **Discovery surfaces** — a daily-rotating spotlight, a "new this week" feed,
  and faceted tag/location pages.

Every fact rendered on a company page has a recorded source, and the model is
instructed to leave a field blank rather than guess — unknown values stay unknown.

## Stack

- **Pipeline:** Python 3.11+ (managed with `uv`), SQLAlchemy 2.x async + Alembic,
  Postgres 15 on Supabase (free tier).
- **Enrichment:** DeepSeek (`deepseek-chat`, OpenAI-compatible API) — a paid API;
  every response is validated against a Pydantic model.
- **Web:** Next.js 16 (App Router), React 19, TypeScript strict, Tailwind v4,
  deployed on Vercel.
- **Automation:** GitHub Actions cron (the repo is public, so Actions are free
  and unlimited).

## Quick start

### Pipeline (Python)

```sh
cd pipeline
cp .env.example .env        # fill in DATABASE_URL, SEC_USER_AGENT, DEEPSEEK_API_KEY
uv sync                     # install deps
uv run alembic upgrade head # create schema
uv run pytest               # run tests

# Discovery: seed companies from VC portfolios + funding news
uv run nous refresh-vc-portfolios   # scrape VC portfolio pages; records the discovering firm as a company investor
uv run nous ingest-news             # Google News + TechCrunch / SiliconANGLE / PR Newswire / Crunchbase News sweep

# Enrich each company with a description + location/industry
uv run nous resolve-homepages            # find each company's website
uv run nous scrape-homepages             # cache homepage + about/product pages (extracted text)
uv run nous enrich-companies --limit 50  # LLM: description, people/leadership, hq city/state, industry

# De-duplicate: merge companies sharing a website domain (LLM-gated for fuzzy matches)
uv run nous dedup-companies
```

Enrichment uses **DeepSeek** (`deepseek-chat`, OpenAI-compatible API), a paid
service — set `DEEPSEEK_API_KEY`. It replaced Gemini, whose free tier was too low
for bulk enrichment (see `nous-technical-spec.md` §3). Each company costs roughly
one LLM call; the pipeline bounds spend with small per-run `--limit`s.

A local Postgres 15 in Docker is the easiest way to develop without touching Supabase:

```sh
docker run --rm -d --name nous-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:15
```

### Web (Next.js 16)

```sh
cd web
cp .env.local.example .env.local   # then fill in SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
npm install
npm run dev
```

Open <http://localhost:3000>. Each company page renders the description, key
facts, leadership/people, funding history, **Investors** (the firms backing the
company), competitors, related companies, and a **News** section drawn from
collected `news_articles`.

## Pipeline stages

Stages are idempotent [Click](https://click.palletsprojects.com/) commands —
re-running one never duplicates or corrupts data. Invoke any of them with
`uv run nous <stage>`.

**Discovery & ingestion**

| Stage | What it does |
|---|---|
| `refresh-vc-portfolios` | Scrape the public portfolio pages of 13 VC firms (Y Combinator, a16z, Sequoia, Lightspeed, Founders Fund, Greylock, Khosla, Bessemer, Accel, Index Ventures, Kleiner Perkins, General Catalyst, Felicis); create companies and record the discovering firm as a company-level investor. |
| `ingest-news` | Per-company Google News RSS plus a broad sweep of TechCrunch, SiliconANGLE, PR Newswire, and Crunchbase News; auto-create newly funded companies found in the sweep. |
| `resolve-homepages` | Find each company's website (TLD probing, then DuckDuckGo fallback), validated against parked/aggregator pages. |
| `scrape-homepages` | Fetch the homepage plus a few about/product/team pages and cache their extracted visible text (headless Chromium fallback for JS-heavy sites). |

**Enrichment (LLM)**

| Stage | What it does |
|---|---|
| `enrich-companies` | Short/long description, category, tags, people/leadership, HQ city/state, founding year (null when unknown). |
| `judge-eligibility` | Decide whether a company is a US software startup; soft-exclude non-US / not-a-startup rows. |
| `extract-funding` | Parse funding rounds, amounts, valuations, and investors from news articles. |
| `extract-funding-website` | Gap-fill funding from a company's own site when news coverage found none. |
| `analyze-competitors` | Two-pass ranked competitor list, grounded in TechCrunch coverage and same-industry peers. |
| `estimate-employees` | Estimate a headcount range from public sources (The Org, GrowJo, careers page, GitHub org, Wellfound). No LLM. |

**Graph, dedup & taxonomy**

| Stage | What it does |
|---|---|
| `link-competitors` | Resolve dangling competitor → company links by trigram name similarity. No LLM. |
| `derive-relationships` | Rebuild the company relationship graph (competitor edges + same-industry "similar" edges). No LLM. |
| `dedup-companies` | Merge duplicate companies: auto-merge on a shared website domain, LLM `company-match` adjudicator for fuzzy matches (high-confidence only). |
| `dedup-investors` | Merge duplicate investors by canonical name; classify known firms as institutional. |
| `normalize-taxonomy` | Canonicalize free-text `industry_group` / `primary_category` values. No LLM. |

**Maintenance & observability**

| Stage | What it does |
|---|---|
| `repair-catalog`, `repair-wrong-websites`, `repair-duplicate-rounds` | Idempotent self-healing passes for known data-quality issues (bad scraped websites, parked-domain enrichments, duplicate rounds). |
| `refresh-latest-round`, `refresh-investor-counts` | Recompute the denormalized latest-round and investor portfolio-count columns. |
| `snapshot-companies` | Weekly per-company snapshot (headcount + trailing-30-day news count) for momentum signals. |
| `db-stats`, `pipeline-health` | Report DB size against the free-tier cap; flag stages whose latest run was empty or errored. |
| `exclude-company` / `unexclude-company` | Manually exclude (or re-include) a company from the catalog by slug. |

## How it runs

Three GitHub Actions workflows, all serialized through a shared `concurrency`
group so they never write the `companies` table at the same time:

- **`pipeline.yml`** (every 3 hours, 8×/day) — the funding-news + enrichment loop:
  repair/normalize passes, then `ingest-news` → `extract-funding` →
  `extract-funding-website` → `refresh-latest-round` → `resolve-homepages` →
  `scrape-homepages` → `enrich-companies` → `judge-eligibility`, then `db-stats`
  + `pipeline-health` and a Vercel deploy. Every stage is idempotent and bounded
  by a small `--limit` / `--max-runtime-minutes`, so repeated runs re-pay almost
  nothing and steady-state runs no-op in minutes.
- **`discovery.yml`** (weekly, Mon 02:00 UTC) — the heavier, least time-sensitive
  work: `refresh-vc-portfolios` → `dedup-investors` → `refresh-investor-counts` →
  `dedup-companies` → `analyze-competitors` → `link-competitors` →
  `derive-relationships` → `estimate-employees` → `snapshot-companies`.
- **`backfill-discovery.yml`** (manual, `workflow_dispatch`) — a one-shot seed:
  `refresh-vc-portfolios` + `ingest-news` over a wider lookback.

`lint.yml` runs on every push and PR: `ruff`, `mypy`, `alembic upgrade head`, and
`pytest` for the pipeline; `lint`, `build`, and a Playwright smoke test for the web.

## Deploy

1. **Supabase project.** Create a free-tier project. Use the Session pooler
   `DATABASE_URL` (rewritten to `postgresql+psycopg://`).
2. **Vercel.** Connect the GitHub repo, set the root directory to `web/`, add env
   vars `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Vercel auto-deploys on
   push to `main`; the pipeline also pings a deploy hook after each successful run.
3. **GitHub Actions secrets.** Add `DATABASE_URL`, `SEC_USER_AGENT`, and
   `DEEPSEEK_API_KEY` so the scheduled crons can run every stage.
4. **First backfill.** Trigger the `backfill-discovery` workflow manually
   (`workflow_dispatch`) to seed companies from VC portfolios + funding news.
