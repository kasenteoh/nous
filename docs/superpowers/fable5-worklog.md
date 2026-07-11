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

## PR #132 — W-E.3 + W-C.1: shared per-domain throttle (merged 2026-07-10)

- New `pipeline/src/nous/sources/_http.py`: `DomainThrottle` (per-domain
  asyncio locks + monotonic timestamps, process-wide `DEFAULT_THROTTLE`
  registry, injectable for tests) and `ThrottledHTTPClient` (throttled GET +
  the shared tenacity policy: retry 429/5xx/timeouts, never ConnectError).
- Fixes the verified W-C.1 bug: `HomepageClient`, `HeadlessBrowserClient`,
  and `NewsClient` each kept per-instance lock dicts, so two transports
  double-hit a host despite docstrings claiming cooperation. All three now
  share the default registry; the curl_cffi Chrome-impersonation fallback
  and the Playwright path pay the same per-domain toll.
- Behavior deliberately unified: the timestamp now stamps in a `finally`
  (failed requests count against the interval — strictly more polite).
- 9 new tests incl. the headline regression: a `HomepageClient` and a
  `HeadlessBrowserClient` hitting one host never fire closer than the min
  interval. Suite: 805 passed. Authored by a worktree subagent; reviewed by
  the orchestrator before ship.

## PR #133 — W-C.5 + W-C.6: current-state docs + backfill runbook (merged 2026-07-10)

- CLAUDE.md: migrations are hand-written (the `--autogenerate` guidance was
  stale and dangerous — autogenerate drops trigram/partial/unique indexes);
  documents the real cron cadence (3-hourly pipeline + weekly discovery) and
  scopes the free-tier rule around the standing DeepSeek exception.
- `nous-technical-spec.md`: the Form-D banner became a full current-state
  banner (discovery spine = VC portfolios + news, DeepSeek runtime, cadence,
  migration convention, pointers to README/CLAUDE.md).
- W-C.6: `pipeline.yml` gains a `run_rejudge_nonstartup` dispatch input
  wiring the existing `judge-eligibility --rejudge-nonstartup-signals` flag
  (workflow now at GitHub's 25-input cap — the next input added must
  displace one); the bounded sweep procedure for the non-US + non-startup
  drains is documented in `docs/runbooks/non-us-and-nonstartup-backfill.md`.
