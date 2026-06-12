# nous

nous is a free, public-facing site for discovering and reading about US software startups, aggregating VC portfolio listings, funding news, public web sources, and LLM-enriched summaries into readable company pages. See [nous-technical-spec.md](nous-technical-spec.md) for the full product and technical design, and [CLAUDE.md](CLAUDE.md) for working conventions in this repo.

## Quick start

### Pipeline (Python)

```sh
cd pipeline
cp .env.example .env       # fill in DATABASE_URL, SEC_USER_AGENT, GEMINI_API_KEY
uv sync                    # install deps
uv run alembic upgrade head  # create schema
uv run pytest              # run tests

# Discovery: seed companies from VC portfolios + funding news
uv run nous refresh-vc-portfolios   # scrape VC portfolio pages; records the discovering firm as a company investor
uv run nous ingest-news             # Google News + TechCrunch venture sweep

# Enrich each company with a description + location/industry
uv run nous resolve-homepages   # find each company's website
uv run nous scrape-homepages    # cache homepage + about/product pages
uv run nous enrich-companies --limit 50   # LLM: description, people/leadership, hq city/state, industry

# De-duplicate: merge companies sharing a website domain (LLM-gated for fuzzy matches)
uv run nous dedup-companies
```

### Pipeline stages

| Stage | What it does |
|---|---|
| `refresh-vc-portfolios` | Scrape registered VC portfolio pages; create companies and record the discovering firm (e.g. Sequoia → "Sequoia Capital") as a company-level investor. |
| `ingest-news` | Google News RSS per company + TechCrunch venture broad sweep; auto-create companies found in TechCrunch. |
| `resolve-homepages` | Find each company's website (TLD patterns, DuckDuckGo fallback). |
| `scrape-homepages` | Cache homepage + about/product/team pages as raw HTML. |
| `enrich-companies` | LLM: short/long description, people/leadership, plus HQ city/state and industry (null when unknown). |
| `extract-funding` | LLM: parse funding rounds, valuations, and investors from news articles. |
| `analyze-competitors` | LLM: ranked competitor list (sourced as TechCrunch vs LLM-inferred). |
| `dedup-companies` | Merge duplicate companies: auto-merge on shared website domain (shared-hosting blocklist), LLM `company-match` adjudicator for fuzzy matches (similar name / shared HQ / similar description), high-confidence only. |

Get a free Gemini API key at <https://ai.google.dev/> (no credit card). The enrichment stage costs ~1 LLM call per company and stays well under the free tier's 1500/day limit.

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

Open <http://localhost:3000>. Each company page renders description, key facts, leadership/people, funding history, **Investors** (the firms backing the company), competitors, and a **News** section drawn from collected `news_articles`.

## Deploy (Milestone 1)

1. **Supabase project.** Create a free-tier project. Use the Session pooler `DATABASE_URL` (rewritten to `postgresql+psycopg://`).
2. **Vercel.** Connect the GitHub repo, set the root directory to `web/`, add env vars `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Vercel auto-deploys on push to `main`.
3. **GitHub Actions secrets.** Add `DATABASE_URL`, `SEC_USER_AGENT`, and `DEEPSEEK_API_KEY` so the scheduled crons can run all stages (homepage + enrich + news + funding).
4. **First backfill.** Trigger the `backfill-discovery` workflow manually (`workflow_dispatch`) to seed companies from VC portfolios + TechCrunch.

Three scheduled workflows then keep the data fresh on their own, serialized via a shared `concurrency` group so they never write the `companies` table at the same time:

- **`discovery.yml`** (weekly, Mon 02:00 UTC) — `refresh-vc-portfolios` → `dedup-companies` → `analyze-competitors`.
- **`descriptions.yml`** (weekly, Mon 06:00 UTC) — `resolve-homepages` → `scrape-homepages` → `enrich-companies`.
- **`funding-news.yml`** (daily, 14:00 UTC) — `ingest-news` → `extract-funding` → `extract-funding-website`.
