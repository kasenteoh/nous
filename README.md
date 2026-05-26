# nous

nous is a free, public-facing site for discovering and reading about US software startups, aggregating SEC Form D filings, public web sources, and LLM-enriched summaries into readable company pages. See [nous-technical-spec.md](nous-technical-spec.md) for the full product and technical design, and [CLAUDE.md](CLAUDE.md) for working conventions in this repo.

## Quick start

### Pipeline (Python)

```sh
cd pipeline
cp .env.example .env       # then fill in DATABASE_URL and SEC_USER_AGENT
uv sync                    # install deps
uv run alembic upgrade head  # create schema
uv run pytest              # run tests
uv run nous ingest-filings --since 2026-04-26   # 30-day backfill
```

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
3. **GitHub Actions secrets.** Add `DATABASE_URL` and `SEC_USER_AGENT` so the weekly cron can run.
4. **First backfill.** Trigger the `weekly-pipeline` workflow manually (`workflow_dispatch`) with `since=2026-04-26` to seed 30 days of filings.

The weekly cron (every Monday 09:00 UTC) then keeps the data fresh on its own.
