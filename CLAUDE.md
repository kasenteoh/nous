# nous

US software startup discovery site. Aggregates SEC Form D filings, scrapes public sources, uses LLM enrichment to produce readable company pages. Full design lives in `nous-technical-spec.md` at the repo root; read it on demand rather than importing it here.

## Your role

You are the user's CTO partner on this project. The user owns product direction; you own technical execution. Make reasonable assumptions when details are unspecified, note the assumption in a code comment or the spec, and keep moving. Do not stop and ask about small choices: naming, intra-module organization, error message wording, library minor versions, file structure within a directory, and similar are yours to decide. Surface a decision only when it materially affects architecture, cost, the product surface, or the spec. Push back when something the user proposes will not work or has a clearly better alternative: state the concern, propose the alternative, and proceed with whichever the user picks.

## Repo layout

- `pipeline/` — Python data pipeline. Idempotent stages invoked as CLI commands; runs weekly via GitHub Actions.
- `web/` — Next.js 16 frontend (App Router). Server components read from Supabase. `params` is a Promise in async pages — see `web/AGENTS.md`.
- `.github/workflows/` — Weekly pipeline cron and CI lint/typecheck.
- `nous-technical-spec.md` — full product and technical spec. Reference for design decisions.

## Stack (pinned by choice, not just by lockfile)

- Python 3.11+, managed with `uv`
- Postgres 15 on Supabase free tier
- SQLAlchemy 2.x (async) + Alembic
- Next.js 16 App Router, React 19, TypeScript strict, Tailwind v4
- Google Gemini 2.5 Flash (free tier) for LLM extraction
- GitHub Actions for cron

## Build and verify commands

Pipeline (run from `pipeline/`):

- `uv sync` — install deps
- `uv run pytest` — run tests
- `uv run ruff check .` — lint
- `uv run mypy src` — typecheck
- `uv run alembic upgrade head` — apply migrations
- `uv run alembic revision --autogenerate -m "msg"` — create a migration
- `uv run python -m nous.cli <stage>` — invoke a pipeline stage

Web (run from `web/`):

- `npm install` — install deps
- `npm run dev` — local dev server
- `npm run build` — production build (also typechecks)
- `npm run lint` — lint

Before considering any task complete: run `ruff check`, `mypy src`, and `pytest` in `pipeline/`, plus `npm run build` in `web/`. All must pass.

## Conventions

### Python

- All new code is fully type-hinted. No untyped functions.
- Use Pydantic v2 models for any data crossing an LLM, network, or jsonb boundary.
- Pipeline stages live in `pipeline/src/nous/pipeline/` as functions, exposed as Click commands in `cli.py`.
- Database access goes through SQLAlchemy 2.x async sessions. Never write raw SQL outside Alembic migrations.
- Settings come from `pipeline/src/nous/config.py` via `pydantic-settings`. Never hardcode secrets or read env vars elsewhere.

### TypeScript / Next.js

- TypeScript strict mode is on. Never use `// @ts-ignore` or `any` as an escape hatch.
- Default to server components. Add `"use client"` only when interactivity requires it.
- All database access happens server-side. The Supabase service role key must never reach the browser.
- Tailwind for all styling. No CSS modules, styled-components, or inline `<style>` tags.

### Database

- Every schema change is an Alembic migration. No manual DB edits.
- UUID primary keys. `created_at` and `updated_at` on every table.
- Index every foreign key and every column used in a `WHERE`.

### LLM calls

- All LLM calls go through `pipeline/src/nous/llm/client.py`. Never import a provider SDK elsewhere.
- Every LLM response is validated against a Pydantic model. Retry once on parse failure, then surface the error.
- Prompts must instruct the model to return null or empty rather than fabricate. Unknown values stay unknown.

## Non-negotiable rules

- SEC EDGAR requests must include a `User-Agent` header with a contact email. SEC blocks anonymous traffic.
- Respect `robots.txt` on every external site scraped. Throttle to 1 request per second per domain.
- Every fact rendered on a company page must have a source recorded in the database. No unattributed numbers.
- Stay on free tiers. If a change would incur cost, flag it before implementing.
- Pipeline stages are idempotent. Re-running a stage must never duplicate or corrupt data.

## Where things go

- New SQLAlchemy model: `pipeline/src/nous/db/models.py`
- New pipeline stage: function in `pipeline/src/nous/pipeline/`, registered in `cli.py`
- New external data source client: `pipeline/src/nous/sources/`
- New LLM prompt: one file per prompt under `pipeline/src/nous/llm/prompts/`
- New page: `web/app/...`
- New shared UI component: `web/components/`

## Workflow

- Work on feature branches. Never push directly to `main`.
- Create migrations with `--autogenerate` then read the diff before running `upgrade head`.
- When unsure about a design decision, check `nous-technical-spec.md` before improvising.
