# Fable 5 worklog — 2026-07-10 improvement plan

Running log of the `fable5/*` PR series executing
[the improvement plan](plans/2026-07-10-fable5-coding-improvements.md).
One entry per merged PR, newest last. Worklog entries are committed directly
to `main` as post-merge bookkeeping (docs-only) so parallel workstream
branches never conflict on this file.

Conventions in effect for the series: CI (`lint.yml`) green before every
merge; squash-merge to match repo history; migrations hand-written;
authoring and review are separate passes (workstreams are implemented by
subagents in isolated worktrees or by the orchestrator, and reviewed by the
other party before ship). Commit trailers use `Co-Authored-By: Claude Opus
4.8 <noreply@anthropic.com>` exactly as specified in the master prompt —
noting that the executor is Claude Fable 5, so the trailer's model name is
inherited from the prompt, not a claim about which model wrote the code.

## PR #131 — W-B: secret-leak prevention (merged 2026-07-10)

- gitleaks full-history CI gate (`secrets` job in lint.yml); config extends
  default rules with **no** path allowlists — the two known false positives
  (public Segment writeKey + reCAPTCHA siteKey inside checked-in scraped-page
  fixtures) are fingerprint-suppressed in `.gitleaksignore`. Opt-in local
  pre-commit hook documented in README "Secret hygiene".
- `npm run check:bundle`: scans every client-visible build artifact
  (`.next/static/**` + prerendered `.html`/`.rsc`/`.body`) for server env
  identifier names and for canary secret *values* that CI now plants at build
  time (`SUPABASE_URL` deliberately stays unset so the secret-free smoke
  contract holds).
- `lib/db.ts` / `lib/queries.ts` now `import "server-only"` — a client-graph
  import is a build failure, not a comment. Boundary documented in
  `web/AGENTS.md`.
- `.gitignore` now covers all `.env` variants (`.env.production` etc. were
  previously uncovered).
- Verified by exercising all three failure modes (client import of db.ts →
  build fails; identifier in client component → bundle scan fails; planted
  PAT-shaped string → gitleaks flags).
- Integration note for W-A: vitest configs that import `lib/queries.ts` must
  alias `server-only` to a stub (the W-A branch was told mid-flight).
