# nous

US software startup discovery site. Discovers companies from VC portfolio pages and funding news, scrapes public sources, uses LLM enrichment to produce readable company pages. Full design lives in `nous-technical-spec.md` at the repo root; read it on demand rather than importing it here.

## Your role

You are the user's CTO partner on this project. The user owns product direction; you own technical execution. Make reasonable assumptions when details are unspecified, note the assumption in a code comment or the spec, and keep moving. Do not stop and ask about small choices: naming, intra-module organization, error message wording, library minor versions, file structure within a directory, and similar are yours to decide. Surface a decision only when it materially affects architecture, cost, the product surface, or the spec. Push back when something the user proposes will not work or has a clearly better alternative: state the concern, propose the alternative, and proceed with whichever the user picks.

## Repo layout

- `pipeline/` — Python data pipeline. Idempotent stages invoked as CLI commands; runs via GitHub Actions cron (`pipeline.yml` every 3h for news/enrichment, `discovery.yml` weekly for discovery/dedup/competitors).
- `web/` — Next.js 16 frontend (App Router). Server components read from Supabase. `params` is a Promise in async pages — see `web/AGENTS.md`.
- `.github/workflows/` — Weekly pipeline cron and CI lint/typecheck.
- `nous-technical-spec.md` — full product and technical spec. Reference for design decisions.

## Stack (pinned by choice, not just by lockfile)

- Python 3.11+, managed with `uv`
- Postgres 15 on Supabase free tier
- SQLAlchemy 2.x (async) + Alembic
- Next.js 16 App Router, React 19, TypeScript strict, Tailwind v4
- DeepSeek (`deepseek-chat`, OpenAI-compatible API) for LLM extraction — paid;
  replaced Gemini, whose free tier (20 RPD) was too low for bulk enrichment
  (see `nous-technical-spec.md` §3)
- GitHub Actions for cron

## Build and verify commands

Pipeline (run from `pipeline/`):

- `uv sync` — install deps
- `uv run pytest` — run tests
- `uv run ruff check .` — lint
- `uv run mypy src` — typecheck
- `uv run alembic upgrade head` — apply migrations
- `uv run alembic revision -m "msg"` — create an empty migration to hand-write (never `--autogenerate`; see Database conventions)
- `uv run python -m nous.cli <stage>` — invoke a pipeline stage

Web (run from `web/`):

- `npm install` — install deps
- `npm run dev` — local dev server
- `npm run build` — production build (also typechecks)
- `npm run lint` — lint

Before considering any task complete: run `ruff check`, `mypy src`, and `pytest` in `pipeline/`, plus `npm run lint`, `npm run test`, and `npm run build` in `web/`. All must pass. DB-gated pipeline tests skip without `DATABASE_URL` (~500 skips is the healthy no-DB baseline) — they run in CI's Postgres service, so a green local run does NOT prove them; check the PR's full check rollup before merging.

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
- Every prompt that persists data carries a `PROMPT_VERSION` constant (scheme `YYYY-MM-DD.N`). Bump it on ANY semantic change, and check what the bump re-selects (`--redescribe-outdated` keys on the long-description prompt's version).
- Prompt edits are gated by the golden set (`pipeline/tests/golden/`, `uv run nous eval-prompts`): CI replays committed recordings against metric floors; re-record live via the `eval-record` workflow (the DeepSeek key exists only in Actions) and review the delta table before committing recordings.

### Embeddings

- fastembed lives in the optional `embeddings` dependency group — `uv sync --group embeddings` where needed (pipeline.yml/discovery.yml do this); plain `uv sync` stays light.
- CI's Postgres service image is `pgvector/pgvector:pg15` (migration 0033 CREATEs the `vector` extension). Local DB-gated runs need the same image.
- Query-side embedding on the web (`web/lib/embed-query.ts`) runs the SAME model with CLS pooling — pooling and model parity with stored vectors are load-bearing; never change one side alone.

## Non-negotiable rules

- Every outbound scrape must send a `User-Agent` header with a contact email (the `SEC_USER_AGENT` setting). Many sites block anonymous traffic, and it is basic scraping etiquette.
- Respect `robots.txt` on every external site scraped. Throttle to 1 request per second per domain.
- Every fact rendered on a company page must have a source recorded in the database. No unattributed numbers.
- Stay on free tiers, with one standing exception: DeepSeek LLM calls are paid (see Stack). Any *new* cost — a new paid API, a tier upgrade, a change that materially raises DeepSeek volume — gets flagged before implementing.
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
- Write migrations **by hand** — never `--autogenerate`. Autogenerate cannot model the trigram/partial/unique indexes this schema depends on and silently drops them (see the docstring warning repeated in every migration from 0015 on). Chain the revision off the current head and write both `upgrade()` and `downgrade()`.
- When unsure about a design decision, check `nous-technical-spec.md` before improvising.

## Keeping the docs current

Doc upkeep is part of "done." Before a task — or a group of related tasks — is
complete, update the doc(s) the work touched. A change that ships code but
leaves these stale is not finished.

| Doc | Update when | How |
|-----|-------------|-----|
| `BACKLOG.md` (root) | Every task / PR | Annotate shipped items `SHIPPED (#PR)`; close by deleting; add newly-discovered work at the bottom of the right section |
| `ROADMAP.md` (root) | A strategic bet ships or is reprioritised, or direction shifts — **not** per task | Move items between Now / Next / Later; annotate with PR#; delete once fully absorbed |
| `docs/superpowers/HANDOFF.md` | End of a work session or group of tasks | Refresh current prod state, in-flight work, and gotchas for the next agent |
| `docs/superpowers/fable5-worklog.md` | Each PR in a series | Append the PR entry in the existing worklog format |
| `CLAUDE.md` / `README.md` / `nous-technical-spec.md` | Conventions, commands, architecture, or a design decision change | Edit the affected section |

Roadmap vs backlog when in doubt: **roadmap = why / what order (bets),
backlog = what next (tasks).** A roadmap bet becoming concrete work means a new
`BACKLOG.md` entry, not roadmap detail.
