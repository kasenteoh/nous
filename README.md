# nous

nous is a free, public-facing site for discovering and reading about US software startups, aggregating SEC Form D filings, public web sources, and LLM-enriched summaries into readable company pages. See [nous-technical-spec.md](nous-technical-spec.md) for the full product and technical design, and [CLAUDE.md](CLAUDE.md) for working conventions in this repo.

## Quick start

### Pipeline (Python)

```sh
cd pipeline
cp .env.example .env       # fill in DATABASE_URL, SEC_USER_AGENT, GEMINI_API_KEY
uv sync                    # install deps
uv run alembic upgrade head  # create schema
uv run pytest              # run tests

# M1: ingest SEC filings
uv run nous ingest-filings --since 2026-04-26   # 30-day backfill

# M2: enrich each company with a description
uv run nous resolve-homepages   # find each company's website
uv run nous scrape-homepages    # cache homepage + about/product pages
uv run nous enrich-companies --limit 50   # call Gemini, write description
```

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

Open <http://localhost:3000>.

## Deploy (Milestone 1)

1. **Supabase project.** Create a free-tier project. Use the Session pooler `DATABASE_URL` (rewritten to `postgresql+psycopg://`).
2. **Vercel.** Connect the GitHub repo, set the root directory to `web/`, add env vars `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Vercel auto-deploys on push to `main`.
3. **GitHub Actions secrets.** Add `DATABASE_URL`, `SEC_USER_AGENT`, and `GEMINI_API_KEY` so the weekly cron can run all stages (ingest + homepage + enrich).
4. **First backfill.** Trigger the `weekly-pipeline` workflow manually (`workflow_dispatch`) with `since=2026-04-26` to seed 30 days of filings.

The weekly cron (every Monday 09:00 UTC) then keeps the data fresh on its own.
