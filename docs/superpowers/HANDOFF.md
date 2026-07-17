# Handoff — state of the world as of 2026-07-17 (end of day)

Written for the next agent (any model) picking this project up cold. Read
this, then root `CLAUDE.md` (conventions), then the worklog
(`docs/superpowers/fable5-worklog.md` — one entry per merged PR, the
authoritative history), then `BACKLOG.md` (annotated with what shipped; its
**"2026-07-16 fresh customer-perspective QA"** section is the current work
queue). The plan docs under `docs/superpowers/plans/` are historical context.

## LATEST UPDATE — the whole QA queue shipped (2026-07-17, PRs #216–#223)

The 2026-07-16 QA queue's P0 + P1 + re-fetch arcs are DONE, merged, and
APPLIED on prod; main is green; no schema changes (migration head stays
**0044**). One-line map (worklog has full entries):

**P0 — aggregation-without-dedup (#216–#218):** suspect-duplicate-rounds
census in data-quality; repair-duplicate-rounds cron-promoted + near-amount
(±15%) + evidence-gated type-conflict merges; GN headline-variant article
dedup. **First cron apply cleaned ~140 junk rounds** (terrafirma
$100M→$115M, sambanova's 9-rounds-for-one-event → the dated Series F).

**P1 — aardvark class (#219–#222):** funding-subject context guard for
single-common-word names; cloudflareaccess/cdn-cgi reject (heals away's JWT
URL); wrong-company reset now clears people/competitors/industry/HQ/
embedding + pass (f) residue drain; retroactive repair-misattributed-news
purge (ops.yml dry-run/apply lever) with two precision spares from the prod
dry-run review. **APPLIED 2026-07-17: 2,861 wrong-entity articles + 35
rounds deleted across 577 companies** (dry-run and apply matched exactly).
helix now carries ONLY the real $10B Selipsky round as a clean husk; the
/trends media-entertainment misfile is resolved in-DB (site follows ISR).

**Re-fetch path (#223):** `verify-sources --refetch` — refetch-bucket facts
get one polite transient live fetch (robots/UA/throttle/SSRF; text never
persisted). Opt-in (CLI flag + verify-sources.yml `refetch` input); cron
untouched. **Drained on prod 2026-07-17:** dry-run + two applies verified
~106 facts (38 via live fetch; 63 supported / 15 unsupported / 17
uncertain persisted this session), the final apply saw 54 < limit 60 → the
addressable pool is near-empty. **One fabrication flag total** (omen-ai — a
model near-quote with an appended "today"; correctly rejected → uncertain,
never a ✓; the guard doing its job). Fetch failures (~6 robots/4xx) carry
no verdict row and re-select next run by design — never verify against
unread text.

**Observability SHIPPED 2026-07-17 (PRs #226–#227):** the public **/stats**
freshness page (latest run per stage from pipeline_runs; 1h ISR; footer
"Status" link) + cron failure alerting (`pipeline-health --strict-errors`
→ a deduped `pipeline-failure` GitHub issue per workflow; closing re-arms).
Load-bearing gotcha pinned in both workflows: id'd always-success steps must
sit AFTER the Vercel deploy gate. Remaining platform-health: optional Sentry
(needs the owner's DSN).

**Embedding/Vercel decoupling CLOSED 2026-07-17 (owner decision: status quo,
#228):** the size gate's first CI run caught the deployed function at ~406MB
(onnxruntime's linux-only CUDA postinstall into the force-included dir —
darwin always measured ~92MB); fixed via `web/.npmrc`
onnxruntime-node-install=skip + cuda/tensorrt excludeGlobs → ~105MB, and CI
now gates at 180MB with a can't-false-pass sanity floor. With Vercel's
Large Functions beta (5GB since 2026-06-29, auto-enroll for new projects)
the outage class is retired. **Escape hatch, documented not built:**
Cloudflare Workers AI serves `@cf/baai/bge-small-en-v1.5` with
`pooling: "cls"` free at ~1000x our volume — the only offload preserving
the embedding space; needs a new vendor account + a ~50-text cosine-parity
spike vs stored fastembed vectors before any trust (Supabase Edge Functions
disqualified: gte-small/mean-pool only → full corpus re-embed). Revisit
ONLY if the beta's GA terms turn hostile or webpack lock-in starts to bite.
**VERIFY next session:** the first post-#228 Vercel deploy's function size
(~400→~105MB expected).

**Follow-ups CLOSED 2026-07-17 (PRs #224–#225):** the cron verify step is
now `--limit 40 --refetch`; the valuation rule is scoped to closed rounds
(funding_extraction 2026-07-17.1, live-re-recorded, all floors green); the
mixed completed/in-talks golden case is live-recorded `supported`; /trends
carries the coverage caveat and /new day buckets a UTC tag; /vs-sitemap and
404-title were policy-closed no-code in BACKLOG. Remaining watch items:
helix/away/amiato re-enrich as the crons re-resolve real sites, and the
uncertain/unsupported recording variance (BACKLOG [S]). **The 2026-07-16 QA
queue is now fully drained — the frontier is platform health** (embedding/
Vercel decoupling, observability: Sentry + pipeline_runs surfacing) and
whatever the owner picks next from ROADMAP Later.

## LATEST UPDATE — three arcs shipped and MERGED (2026-07-15/16, PRs #202–#215)

All fourteen PRs are merged; main is green; migration head **0044** is on
prod; docs/worklog/backlog are current. One-line map (worklog has full
entries):

**Arc 1 — known-issues sweep + verification hardening (#202–#210):**
claim-drift false-✓ fix (pipeline stale-claim sweep + web grammar-anchored
claim guard), momentum exit-cohort clear, `unsupported` verdicts in the
data-quality report, verify-sources in the 3h cron (`--limit 40`),
migration **0044** `news_articles.funding_round_id` + timeline grouping by
the persisted link, ellipsis-aware grounding, sharded sitemaps
(`/sitemap/core.xml` + `companies-<i>.xml`; robots lists every shard).

**Arc 2 — fresh QA pass + AI-answer surfaces (#211–#213):** a 3-lane
customer-perspective QA sweep against prod (findings triaged into BACKLOG);
**/llms.txt + /c/[slug].md** (ROADMAP Later #2 — markdown siblings with
per-fact source URLs + verification annotations, via a next.config rewrite);
QA polish (homepage strip is now momentum-driven "Heating up" with a neutral
fallback, investor pages self-consistent on portfolio counts, export accepts
industry slugs, homepage RSS autodiscovery restored — NB: page-level
`alternates` shallow-replaces the layout's); `portfolio_count` now counts
the SHOWN cohort (pipeline + web aligned).

**Arc 3 — QA P0 forensics + rumor guard (#214–#215):** the "merged-entity
corruption" was root-caused via prod `inspect-company` dispatches — NOT
dedup: the old resolver accepted news-site ARTICLE URLs as homepages
(helix→machinebrief, away→marketspy, amiato→failory), so enrichment
described the news site and the website-funding gap-fill mined OTHER
companies' rounds off its pages. `repair-wrong-websites` (whose pass (e)
detected the class all along but was never dispatched) now runs EVERY 3h
cron with a double-confirmed same-host round/article purge; the three hosts
are blocklisted; improbable excluded via ops (wrong entity + UK). The rumor
guard hardened BOTH prompts (funding_extraction 2026-07-16.1 +
source_verification 2026-07-16.2) with live re-records — verdict_accuracy
0.888→**0.947**, the funding set's first fully-live recording, and the
version bump auto-strips any existing rumor ✓s via the cron.

**Prod state / self-draining processes (no babysitting):** discovery ran
2026-07-16 → momentum, completeness, and map coords are POPULATED (badge,
/trending, homepage strip, portfolio momentum all live after ISR). The 3h
cron now also runs: `repair-wrong-websites` (poisoned rows heal + purge),
`verify-sources --limit 40` (re-verifying the whole cohort under
2026-07-16.2 — a few days at 40/run, ~$0.30), and repair-catalog pass 4
(news→round links). The funding-extraction version bump does NOT re-extract
old articles (processed-once) — only new extractions use the rumor rule.

## NEXT QUEUE (in priority order — BACKLOG "2026-07-16 QA" section has detail)

1. **Aggregation-without-dedup [M, P0]** — near-amount duplicate rounds
   (terrafirma: one Series A stored as $115M AND $100M; reconcile only
   merges EQUAL amounts) + the same event rendered 8–12× from near-identical
   Google News URLs (sambanova, blue-origin). Husk-style: measure first (a
   $0 probe/data-quality signal for suspect near-duplicates), then a
   conservative merge design (same type + date window + amounts within
   ~15%?), plus canonical-URL normalization for news.google.com articles.
2. **Wrong-entity news attribution (aardvark class) [M, P1]** — /c/aardvark's
   timeline is keyword-scrape garbage; generic dictionary-word names need a
   stronger entity match at ingest-news attribution. Also owns helix's
   surviving third-party-syndicated rounds (the same-host purge deliberately
   spares cross-host URLs).
3. **Verification re-fetch path [M]** — the ~103 refetch-bucket facts
   (scraping etiquette; mirror sources/news.py). Verification files are now
   stable (no in-flight PRs).
4. **Small [S] items:** valuation-rule parenthetical + mixed
   completed/in-talks golden case (batch with the next eval-record);
   /trends coverage-caveat framing; /vs + /alternatives sitemap-or-noindex
   policy; 404 server-rendered title; /new future-date display.
5. **Platform health (standing):** embedding/Vercel decoupling,
   observability (Sentry + pipeline_runs surfacing).

**Verify-after-cron checks worth doing next session:** helix/away/amiato
re-enriched correctly (descriptions + industry fixed → /trends
media-entertainment pollution gone); the data-quality report's unsupported
section post-re-verify; the wrong-site purge counters in the repair step
summary.

## LATEST UPDATE — source-verification SHIPPED (2026-07-15, PRs #197–#201)

The owner-approved **"✓ Verified against source"** enhancement (spec
`specs/2026-07-14-provenance-ui-design.md` → source-verification) is **COMPLETE and
LIVE**. A **discriminative** (never generative) DeepSeek pass verifies each rendered
fact (total raised, non-active status, each funding round) against its cited source
— supported / unsupported / uncertain + a verbatim quote — and `/c/[slug]` shows a
subtle green **✓ for `supported` ONLY** (uncertain/unsupported never a badge; one
false ✓ would kill the moat). Five husk PRs:
- **#197** probe + dry-run gate ($0/measure-first) — the `source_verification`
  prompt (`quote_is_grounded` guard) + prevalence + dry-run + `verify-sources.yml`.
- **#198** schema — **migration head 0043** (`fact_verifications` + model).
- **#199** apply path + golden gate — version+source-gated idempotent upsert;
  refinements (skip NULL-amount rounds, log rejected quote, stored-text only);
  golden set + `score_source_verification` (grounding_min = the no-fabrication gate).
- **#200** web ✓ — `VerifiedBadge` on total raised / status / each round;
  supported-only + source-matched (no stale ✓); migration-order-free.
- **#201** live DeepSeek re-record — verdict_accuracy 0.889, **grounding_min 1.0
  (zero fabrication against the real model)**; baseline re-anchored.

**Prod state:** a limit-25 apply run wrote **25 verdicts (18 grounded supported, 0
false ✓)**. **To widen coverage:** dispatch `verify-sources.yml -f run_apply=true -f
limit=N` (idempotent; the ~691 stored-text addressable facts drain over a few runs;
~$0.0004/fact). The ✓ appears on each `/c/[slug]` after ISR revalidation.

**Follow-ups (BACKLOG, not started):** the **re-fetch path** (the ~103 refetch-bucket
facts, scraping etiquette); surface `unsupported` counts in the `data-quality`
report (internal signal); wire `verify-sources --apply` into a cron cadence once the
one-time backfill drains. Read the worklog's #197–#201 entries +
`pipeline/src/nous/pipeline/verify_sources.py` before extending.

## LATEST UPDATE — Timeline coverage grouping (2026-07-14, PR #194)

Owner-flagged `/c/[slug]` **Timeline clutter** fixed: because `ingest-news` only
ingests funding announcements, the "news" IS the funding coverage, so one
well-covered round rendered as N near-duplicate news rows. New pure
`web/lib/timeline.ts` `buildTimeline` clusters each article UNDER the round it
covers (nearest `announced_date` within ±14d; a round's `primary_news_url` is
PINNED to that round by canonical URL, before nearest-clustering, so a neighbor
round can't steal it and it can't double-render); `EventTimeline` renders ≥2
sources as a collapsed `<details>` "Covered by {outlets} +N more" (every article
one click away — trust-preserving). Read-time only (no migration/pipeline change).
Consolidated the http(s) host parse into `web/lib/url.ts` `httpHost` (SourceLink /
Sources / timeline). **Follow-up:** if the date-proximity mapping proves accurate,
persist a `news_articles.funding_round_id` link for exact grouping.

## LATEST UPDATE — Provenance UI MVP COMPLETE (2026-07-14, PRs #191–#193)

ROADMAP **Later #1 (Provenance UI)** — the owner-approved 3-PR MVP is **SHIPPED**:
the "every fact is sourced" moat is now a visible, positive, trust-building
feature on `/c/[slug]`. The authoritative spec is
`docs/superpowers/specs/2026-07-14-provenance-ui-design.md` (framing, data
inventory, locked decisions, the optional DeepSeek source-verification
enhancement). Framing (load-bearing): a **trust-builder, never a data-gap
advertiser** — completeness ≠ trustworthiness; the badge is positive-only and
hidden below threshold.

**PR #191 (PR 1/3 — pipeline, $0):** the stored completeness score.
- **Migration head is now 0042** (`companies.completeness_score` Float +
  `completeness_computed_at`; off 0041). No index (per-company page read).
- New **`compute-completeness`** stage writes it for every *shown* company via
  `util.completeness` (THE scorer — the web must NOT re-derive it in TS); wired
  into `discovery.yml` after `compute-momentum` with an `id` (deploy-gate). $0,
  deterministic, idempotent. Extracted `completeness_fields()` as the single
  raw→flags mapping (data_quality refactored onto it).
- **Gotcha / design note:** a company that EXITS the shown cohort (loses both
  description and funding, or becomes excluded) has its `completeness_score`
  cleared to NULL — a deliberate divergence from `compute-momentum` so a stale
  "richly documented" badge can never render (a false trust claim). `momentum_score`
  has the same exit-cohort staleness and does NOT clear (accepted; noted as
  shared debt if it ever reads as a trust claim).

**PR #192 (PR 2/3 — web):** the `/c/[slug]` **"Data & provenance"** panel.
- New `ProvenancePanel` server component (before `<Sources>`): positive-only
  completeness badge (`≥0.75` "Richly documented", `0.5–0.75` "Well documented",
  else no badge); **"Last verified N days ago"** = read-time MAX over the present
  freshness stamps (`last_enriched_at` + the `*_checked_at`/`_resolved_at`
  columns), `title` = exact date, omitted when none present; a sourcing line
  anchor-linking to `#sources`. Omit-when-empty; muted `MomentumBadge` vocabulary.
- `getCompanyBySlug` unchanged (`.select("*")` picks up the columns
  post-migration); `CompanyRow` gained 7 optional+nullable fields; absent → hides.
- **Gotcha / design note:** the sourcing line's `hasSources` gate MUST use
  `hasRenderableCitations()` (exported from `Sources.tsx`) — the same
  `hostname()`-survival predicate `<Sources>` filters on — NOT raw
  `citations.length`. `<Sources>` drops citations whose URL fails `new URL()`, and
  the pipeline stores scheme-less bare domains (`company.website` = `acme.com`, the
  total_raised / leadership source fallback), so a raw-length gate showed the "every
  figure links to a recorded source" line + a `#sources` anchor for a company where
  `<Sources>` renders nothing (dead anchor + false claim). Caught in review.

**PR #193 (PR 3/3 — web):** granular per-fact sourcing.
- Inline source superscripts (`SourceLink`): a subtle `↗` next to total-raised /
  status / website / each funding row → that fact's source; **self-omits** when
  the URL is absent or not parseable http(s) (the pipeline stores scheme-less bare
  domains, so a "source" affordance never goes nowhere). Source-type labels in
  `Sources` ("News/Website/Wikidata/VC portfolio" from host + the `website_source`
  enum ground truth; **unknown host → no label**, never a guess). Confidence: a
  `title` tooltip on ALL funding rounds, visible pill only for `low`. `CompanyRow`
  gained `website_source?` / `website_source_url?`.
- **Gotcha 1 (a11y token debt — logged, not fully fixed):** `text-ink-faint`
  (~1.42:1 on light) is used ~30 places for de-emphasized supplementary text —
  below WCAG AA. Fixed the two trust-critical provenance instances (the `↗` glyph
  + the source-type tag → `text-ink-muted`); a **system-wide token pass is a
  separate follow-up** (see BACKLOG).
- **Gotcha 2 (`website_source_url` must be a citation):** the page must push
  `website_source_url` into the `citations` list (like total-raised/status) or the
  Website/Wikidata/VC-portfolio source-type labels are unreachable — the
  `citationSourceType` override keys on that URL's host. Also: `citationSourceType`
  restricts to http(s) (matching `SourceLink`), so exotic-scheme URLs get no tag.
- **Gotcha 3 (`Sources.tsx` NUL byte):** the file carried a pre-existing NUL byte
  (from #192) that made git flag it binary; #193 removed it. If it recurs, diff/
  read with `--text` / `grep -a`.

**Sequencing note (retro):** PRs 2 & 3 were *planned* parallel but shipped
**sequentially** — both edit `web/lib/types.ts` (`CompanyRow`) and
`web/app/c/[slug]/page.tsx`, and disk is too full for a second web worktree's
`node_modules`, so parallel edits in one tree would conflict.

**Remaining on this bet (optional, NOT started — needs owner go-ahead):** the
DeepSeek **source-verification** pass ("✓ Verified against source"). It is a
*material DeepSeek volume* increase, so per `CLAUDE.md` it must be flagged before
building; do it husk-style (a $0/bounded dry-run FIRST to measure verify-rate + $,
golden-set-gate the prompt, scraping etiquette on re-fetches, empty-not-fabricate).
The LLM-*written* provenance narrative stays OFF (generative prose is what the moat
forbids).

## LATEST UPDATE — talent-flow "founder background" rider SHIPPED (2026-07-14, PRs #185–#189)

ROADMAP Next **#4 (talent-flow) is BUILT** — as the evidence-gated per-company
**"founder background / notable alumni" rider**, not the graph (the #184 probe
found named pedigrees too thin/non-catalog for a graph). Five PRs, husk-style
(measure → gate → build):
- **#185** — `extract-career-history --dry-run` + the `career_history` prompt
  (hardened, empty-not-fabricate). **Prod dry run (20 top-funded): 50% yield, 0
  fabrication, $0.025** → cleared the gate.
- **#186** — migration **0040** `career_moves` (schema only; the 3-PR husk split).
- **#187** — the persisting apply path + golden set. Version-gated + idempotent
  (**migration 0041** `career_extracted_prompt_version` stamp so the ~85% empty
  bios aren't re-billed); replace-style writes; `prior_company_id` by exact
  unique normalized-name match. 16-fixture golden gate.
- **#188** — the `/c/[slug]` **Founder background** web rider (grouped by person,
  links to in-catalog priors, omit-when-empty, honest tenure).
- **#189** — live DeepSeek golden re-recording (grounding **1.0**, empty_accuracy
  0.937 — the gate now reflects reality).

**Prod data — backfill COMPLETE.** The whole cohort is drained (827 companies
that have BOTH a leadership roster and scraped pages — the last batch saw
307 < 500, so nothing unstamped remains): **2,106 career_moves rows, 264
in-catalog `prior_company_id` links, 0 persisted fabrication, $1.10 total**
(~$0.0013/company — well under the ~$6.50 estimate; the cohort was smaller than
the 2,210-with-pages because it also requires a `people` roster). Steady-state
re-extraction is **dispatch-only** (no cron wiring, deliberately — a bump of
`PROMPT_VERSION` re-selects everyone; empties are stamped so they don't re-bill),
and as scrape/enrich coverage grows, new roster+page companies are picked up by
re-dispatching `extract-career-history.yml -f dry_run=false -f limit=500`. The
web section appears on each `/c/[slug]` after the 6h ISR revalidation.

**Gotcha logged (career_moves apply):** a per-company `session.rollback()`
expires the WHOLE identity map (independent of `expire_on_commit`), so the loop
drives off company IDs and re-`session.get`s each — never touch a preloaded ORM
object after a sibling rollback (it fires sync IO → `MissingGreenlet` and crashes
the run). Post-rollback logs use a captured slug local.

## LATEST UPDATE — investor depth SHIPPED (2026-07-14, PR #190)

ROADMAP Next **#5 (investor depth) is BUILT** — the last Next bet, so the whole
**Next (depth) horizon is now cleared** (#1 map, #2 momentum, #3 RSS, #4
talent-flow, #5 investor depth all shipped). Turned the investor directory from
a list into a lens, $0 / read-time, from existing linkage:
- **Co-investment** ("frequently co-invests with") already shipped
  (`getCoInvestors`, read-time, capped — no persisted edge, O(N²) to store).
- **Portfolio momentum (#190):** `getInvestorPortfolioMomentum` aggregates the
  `momentum_score` (#181) across an investor's DISTINCT shown portfolio companies
  (unioned over both link paths, deduped by slug) → a "N of M heating up" section
  + the hottest few on `/investor/[slug]`. Omit-when-cold; fetch capped 2000/path
  for mega-funds; migration-order-free. Renders once momentum populates on the
  weekly `discovery.yml` cadence.

**Follow-ups (BACKLOG, unstarted):** "who's leading rounds in industry X right
now" (an `/industry/[group]` surface) + a global co-investment meta-graph. The
frontier is now the **Later** horizon (provenance UI, AI-answer surfaces) + the
cross-cutting platform-health debt (embedding/Vercel decoupling, pipeline.yml
input cap, observability).

## LATEST UPDATE — talent-flow feasibility gate (2026-07-13, PR #184)

ROADMAP Next **#4 (talent-flow) is feasibility-gated, not built.** Rather than
spend LLM budget blind, a $0 read-only `career-history-probe` measured whether
scraped bios carry **named** prior employers. **Prod result (2,210 companies with
pages):** 69.5% have a bio section, but named prior-employer is **only ~18% (SQL
upper bound) / ~13–15% after noise-filtering** — below the ~30% bar for a rich
graph, and many named orgs (Intel/IBM/NVIDIA) are non-catalog non-startups. So
the "Stripe → founders → companies" **graph is not well-supported by current
data**; a per-company "founder background" rider on the ~1-in-6 pages that name a
pedigree is feasible via a bounded LLM extraction (~$6.50 one-time). The
`career-history-probe` tool ships (reusable to re-measure as scrape coverage grows).

**➡️ NEXT SESSION'S QUEUE (owner-approved 2026-07-13):** build the niche
talent-flow **"founder background" rider** first (accepting the new DeepSeek
line, ~$6.50 one-time — start with the ~$0.05 LLM extraction dry run to confirm
quality), THEN pivot to **investor depth (#5)** (co-investment graph from
`funding_round_investors`/`company_investors`, $0). **Full design + cost + the
husk-style dry-run-first method are in
[`docs/superpowers/plans/2026-07-13-talent-flow-rider-and-investor-depth.md`](plans/2026-07-13-talent-flow-rider-and-investor-depth.md)**
— read it before starting. Migration head is **0039**; the next migration is 0040.

## LATEST UPDATE — per-entity RSS feeds shipped (2026-07-13, PR #183)

ROADMAP **Next #3 (per-entity RSS) done** — web-only, $0, works immediately (no
cadence/migration dependency, unlike the map/momentum). The global `/feed.xml`
firehose fanned out to `/c/[slug]/feed.xml`, `/industry/[group]/feed.xml`,
`/investor/[slug]/feed.xml` (route handlers, 6h ISR, `application/rss+xml`,
newest-first funding+news, canonical/slug-gated → 404, shown-cohort only). Shared
`lib/rss-items.ts` mappers (the global feed refactored onto them, byte-identical);
`<link rel="alternate">` + a visible "Follow via RSS" link on each entity page.
Built + adversarially reviewed by 2 agents (APPROVE, 0 blocking). **Remaining Next
bets: talent-flow (#4), investor depth (#5).**

## LATEST UPDATE — momentum signals shipped (2026-07-13, PRs #181/#182)

ROADMAP **Next #2 (momentum / "heating up") done** — the "open it every morning"
hook. Same 6-agent, two-workflow pattern (scout → implement → review), pipeline +
web in parallel, both adversarially reviewed (0 blocking).
- **#181 (pipeline):** `compute-momentum` — weekly `momentum_score ∈ [0,1]`
  (0.5=flat, NULL=insufficient data) as a **weight-renormalized mean over the
  PRESENT components**: news acceleration (0.50, `company_snapshots.news_count_30d`
  recent-vs-baseline), funding recency (0.35, `latest_round_date` exp-decay),
  headcount growth (0.15). Migration **0039** (`momentum_score` partial-DESC
  indexed, `momentum_computed_at`, `momentum_why` text[]). Deterministic
  (anchored to `as_of_week`), $0, weekly in `discovery.yml` after Snapshot
  companies. `--as-of-week` for backfill.
- **#182 (web):** `/trending` ("Heating up") ranked grid + `🔥 Heating up` badge
  (threshold 0.65) + pipeline-worded "why" line. Migration-order-free (empty-state
  until scores land), so independent of #181.
- **Populates:** on the weekly `discovery.yml` run once 0039 reaches prod (next
  pipeline cron applies it). **Launch reality:** `company_snapshots` is new, so
  early scores are funding-recency-dominated until ~6 weekly rows accrue per
  company (self-enriches; no code change).
- **Gotcha logged:** a parallel main-tree agent's branch got reset to main on
  origin mid-run; the work commit survived locally and was restored by
  fast-forward push. Re-verify branch tips (`git ls-remote`) after a main-tree
  agent finishes.

## LATEST UPDATE — market map shipped (2026-07-13, PRs #179/#180)

ROADMAP **Next #1 (market map) done** — the first depth feature after the Now
horizon. Built by 6 agents across two workflows (2 scout → 2 implement → 2
review), pipeline + web in parallel (isolated worktree + main tree), each
adversarially reviewed (both APPROVE, 0 blocking).
- **#179 (pipeline):** `compute-map-positions` — per-`industry_group` scikit-learn
  **PCA(2)** over description embeddings → deterministic (svd_solver="full" +
  pinned sign convention + per-axis min-max) 2D coords in three new nullable
  columns (`map_x`/`map_y`/`map_computed_at`, **migration 0038**). $0 (local CPU,
  reuses the `embeddings` uv group), per-industry TTL-gated (25d) off
  `discovery.yml` → effective monthly. `Projector` Protocol seam (tests inject a
  fake; sklearn not needed to run them).
- **#180 (web):** `/map/[industry]` — a **static server SVG** (no client
  component, **no ML on the Vercel function** — the #157 lesson, proven via build
  traces). Nodes = SVG `<a>` links, funding-sized, canonical-gated, ISR,
  a11y-complete. Queries degrade to an **empty-state** until coords exist
  (migration-ordering-for-free), so the two PRs were independent.
- **To see real maps:** coords populate on the next **`discovery.yml`** run once
  migration 0038 reaches prod (next pipeline cron applies it). Until then every
  map is the empty-state by design. To populate sooner: after 0038 is on prod,
  dispatch `discovery.yml` once (it's TTL-gated, so `compute-map-positions` runs).
- **Deferred follow-ups:** interactive client renderer (d3-force) + theme
  coloring + a global theme-level meta-graph; the per-axis-vs-shared-scale visual
  tuning call (BACKLOG).

## LATEST UPDATE — Now horizon field-normalization + report-data (2026-07-13, PRs #176/#177)

ROADMAP Now **#3 and #4 done** — the data-quality "Now" horizon is now
substantially **complete** (#1–#4 shipped; #5's internal primitive shipped).
Built by **two agents in parallel** (pipeline in an isolated worktree + web in
the main tree — disjoint dirs, no parallel node_modules to blow the near-full
disk), each adversarially reviewed before merge, merged sequentially with docs
consolidated to main after.
- **#176 (pipeline):** `hq_state` canonicalized to the 2-letter USPS code
  (`util/us_state.py` — 50 states + DC, non-US → None → untouched), applied at
  the enrich write-site + a bounded idempotent `normalize-hq-state` backfill
  (`--limit`/`--dry-run`). **Routing-safe:** the code is the only form
  `/location/[state]` resolves (route uppercases the segment), so full-name rows
  that 404 today start resolving. No migration. **Now wired into the 3h cron**
  (`normalize-hq-state --limit 500`, id'd, after normalize-taxonomy) so prod
  drains automatically then no-ops; enrichment normalizes new writes too.
- **#177 (web):** per-company "Report incorrect data" `repoIssueUrl` rider on
  `/c/[slug]`; `formatUsd` exact-dollars `title` tooltips on every individual
  funding figure; `/tag/[tag]` `noindex` when <3 companies (lockstep with the
  sitemap's ≥3 filter).

**What's next:** the Now horizon is cleared and Next #1–#3 shipped (market map
#179/#180, momentum #181/#182, per-entity RSS #183), so the frontier is the tail
of the **NEXT horizon (depth)** — **talent-flow** (#4, from `people`) and
**investor depth** (#5, co-investment networks). Smaller Now follow-ups remain:
run the
`normalize-hq-state` backfill once; wire `util.completeness` into
husk-enrichment ordering; watch the `data-quality` cron report (esp. the
website-provenance / wrong-site proxy from the husk re-mining).

## LATEST UPDATE — data-quality dashboard shipped (2026-07-13, PR #175)

ROADMAP Now **#2 done** (and #5's internal primitive). New read-only
`data-quality` stage — the completeness sibling of db-stats (size) and
pipeline-health (freshness) — emits a step-summary report over the shown cohort:
field-completeness %s, **website provenance by `website_source`** (surfaces the
#174 re-mining contribution + the wrong-site proxy), the per-company
completeness-score distribution (new pure `util.completeness`, weighted 0..1),
duplicate rate, staleness. Id-free cron step next to db-stats (no writes, no
migration). **See the report in the next 3h cron run's Actions step summary** (or
dispatch `pipeline.yml`) for the real completeness numbers — that's the instrument
panel to watch as the remaining Now items ship. Next in the queue is **#3**
(field normalization: `hq_state`, `formatUsd`; and re-enable "report incorrect
data" — highest trust-per-effort). The completeness score is internal-only;
wiring it into husk-enrichment ordering + a public trust badge is a follow-up.

## LATEST UPDATE — husk website re-mining shipped (2026-07-13, PRs #172–#174)

ROADMAP Now #1 is **done**. The `resolve-website-fallback` stage resolves
website-less husks from sources that were never the origin homepage — **Wikidata
"official website"** (P856, name + org-type + country matched) and **outbound
links in already-sourced news article bodies** (re-fetching the article, not the
Cloudflare-origin) — $0, idempotent, provenance recorded per site
(`website_source` + `website_source_url`). **Migration head is now 0037** (also
adds `website_fallback_checked_at`, the stage's own rotation stamp, separate from
resolve-homepages' `website_resolved_at`). It's **live in the 3h cron** (id'd
step before resolve-homepages, `--limit 25`), so prod drains ~25 husks/run
(gradual = safe first application). A **30-husk prod dry run** resolved 37% at
~10/11 precision, 0 conflicts (via `resolve-website-fallback.yml`, the dispatch
lever — dry-run default, also a faster-backfill knob).

Gotchas learned this session:
- **`workflow_dispatch` must be on the default branch to be triggerable**, and a
  migration whose file is absent from the branch the cron runs would crash its
  `alembic upgrade head`. Those two together forced a **3-PR split** (dispatch
  workflow #172 → schema/migration #173 → stage #174) to run a real *pre-merge*
  prod dry run. Keep that ordering for any future stage that needs a pre-merge
  prod measurement + a new migration.
- **`news_articles.raw_content` / `raw_pages.content` store visible TEXT, not
  HTML** — no `<a href>` survives, so link-mining re-fetches the article live.
  And `raw_pages` is company-scoped (not VC-portfolio pages); the portfolio
  adapters already capture `entry.website` at discovery — so a VC-portfolio
  re-mining source is redundant and wasn't built.
- **Residual precision risk:** a NULL-`hq_country` husk with a generic name can
  still match a same-named *foreign* company on Wikidata (the dry run's "Apex
  Technologies" → French "APEX Technologies" case). The country cross-check only
  fires on a *confirmed* conflict (won't drop correct foreign matches like
  Taxfix→.de). Every write is sourced + reversible; the re-enabled "report
  incorrect data" link (Now #4) is the human catch. Watch the wrong-site rate on
  the data-quality dashboard (Now #2).

## LATEST UPDATE — roadmap + data-quality pivot (2026-07-13, PR #171)

The **SEO growth engine** (the initiative in the older "Open items" list) is
now SHIPPED end-to-end on the `0036` RPC foundation (#164): industry pages
(#165), `/trends` (#166), `/vs` + shared `CompareTable` (#167, competitors-embed
fix #168), `/feed.xml` RSS (#169), and the unified `/c` event timeline (#170).
Only the **market map** (old item 5) was left un-built.

A product-strategy pass with the owner then reset direction and added a living
roadmap (#171):
- **`ROADMAP.md` (new, repo root)** — the strategic layer above `BACKLOG.md`, as
  Now / Next / Later horizons. **North star is now DATA QUALITY FIRST, then
  depth** — a deliberate pivot from pure SEO/distribution toward earning trust
  before adding surfaces.
- **"Route around, don't evade"** — the ~890 husk companies (Cloudflare-403'd
  from Actions IPs) get resolved from sources that were never the origin
  homepage (news/portfolio outbound links → Wikidata → Common Crawl). Proxy/
  account/evasion tactics are **rejected on principle** (contradict the sourcing
  moat, rot on Cloudflare updates, unnecessary since husks are prominent).
- **`CLAUDE.md`** gained a **"Keeping the docs current"** convention (doc upkeep
  is part of "done": backlog / roadmap / handoff / worklog).
- **The market map is demoted to the Next horizon;** the data-quality Now horizon
  is the priority. See the reordered "Open items" below and `BACKLOG.md`'s
  "2026-07-13 ROADMAP 'Now' horizon" section.

## LATEST UPDATE — Opus 4.8 session (2026-07-12 → 07-13, ~PRs #157–#164)

Wave 3 is now genuinely LIVE and the next initiative (the SEO growth engine)
is underway. What changed since the "as of 2026-07-12" body below:

- **Frozen-prod recovery (the fire):** prod had been frozen ~a day at the
  pre-E-2 commit — every Vercel build failed because the `/companies`
  serverless function hit Vercel's 250MB limit (415MB). Root cause: Vercel's
  **Turbopack builder ignores `outputFileTracingExcludes`**. Fixed by pinning
  the web build to `next build --webpack` (#157) AND setting
  **`VERCEL_SUPPORT_LARGE_FUNCTIONS=1`** on the Vercel project — **both are now
  REQUIRED; a fresh project/clone must have the env var or deploys fail.**
  Semantic search is finally live (it had never actually deployed).
- **Perplexity / website-less-husk arc (#158–#163):** root-caused two layers —
  no `website` (resolved before the curl_cffi Cloudflare bypass PR #132) AND
  the scrape is **Cloudflare-403'd from Actions datacenter IPs** (both httpx
  and curl_cffi; a 403 short-circuits before the Playwright render). Shipped
  reusable `nous inspect-company` + `nous reresolve-company` (via `ops.yml`),
  db-stats cohort counts (**890 website-less shown companies, 882 re-drainable
  now**), and a self-bounding **re-drain of the pre-#132 cohort** (in flight
  over the crons). The structured-data describe fallback ("A") was designed +
  validated but **deferred** (marginal + an off-page `description_short`
  compliance gap).
- **Product roadmap designed** (multi-agent workflows + adversarial critique),
  owner-approved: **SEO growth engine first, drop A, market map last.** Shipped
  **migration `0036`** — the `funding_by_quarter` + `industry_funding_momentum`
  RPCs (the foundation the industry pages / `/trends` need; verified against a
  local pgvector container, full 1489-test DB suite green). **Migration head is
  now 0036.**
- **New gotcha — local DB verification:** OrbStack is installed and
  `pgvector/pgvector:pg15` is cached. For migration/RPC work, spin one up
  (`docker run -d --name nous-pg -e POSTGRES_PASSWORD=postgres -e
  POSTGRES_DB=nous_test -p 55432:5432 pgvector/pgvector:pg15`;
  `DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:55432/nous_test"`;
  `uv run alembic upgrade head`; `uv run pytest -q` runs all ~1489 DB-gated
  tests) and verify for real instead of round-tripping through CI.

## What just happened (25 merged PRs, #131–#155)

Two initiatives, both complete:

1. **2026-07-10 improvement plan** — web test suite (Vitest+RTL+Playwright),
   LLM eval golden set + harness, shared per-domain HTTP throttle, secret-leak
   prevention (gitleaks + client-bundle canary scan + `server-only`
   boundary), bug sweep (loud Vercel misconfig, one META_LEAK guard, deduped
   total-raised), prompt-version provenance stamps (migration 0031), the W-F
   description rewrite (judge/describe prompt split, ~350–600-word grounded
   profiles), discovery expansion (GeekWire/VentureBeat, uniform adapter
   hard-fail contract), slug aliases + 308 redirects (0032).
2. **Hygiene + Wave 3** — husk rescue (prominent description-less companies
   prioritized + force-rendered), canonical tag vocabulary, word-boundary
   funding keywords + GitHub-trending discovery, then the embeddings stack:
   pgvector + `embed-companies` (0033), themes (0034), semantic search
   (0035).

## Working agreement (user-set, standing)

- User owns product; agent owns technical execution, full autonomy on
  reversible engineering decisions. Stop only for product/architecture
  changes, destructive-beyond-git actions, or true blockers.
- Branch per slice (`fable5/<name>` — adopt your own prefix), PR via `gh`,
  **merge your own PR when CI is green**, squash + delete branch. Verify the
  FULL `statusCheckRollup` JSON explicitly before every merge — piping
  `gh pr checks` through grep/tail once masked a red pipeline job and main
  was red for 13 hours (worklog: "red-main incident"). Never merge red.
- Commit trailer exactly: `Co-Authored-By: Claude Opus 4.8
  <noreply@anthropic.com>` (user-specified; see worklog preamble). PR bodies
  end with the Claude Code attribution line.
- Worklog entry per merged PR; docs-only worklog commits go directly to main.
- DeepSeek is the runtime LLM — never swap it. Cost is not a constraint but
  flag any material spend before incurring it.

## Environment facts (will bite you if unknown)

- **No local Postgres/DB URL/DeepSeek key.** DB-gated tests (~500) skip
  locally and run in CI's Postgres service (`pgvector/pgvector:pg15`).
  A container runtime (OrbStack) exists — recent agents ran the full
  DB-gated suite against a local `pgvector/pgvector:pg15` container; do that
  for migration work if you can.
- **Actions is the only prod lever.** `pipeline.yml` (3-hourly; at GitHub's
  25-input cap — a new input must displace one; prefer new behavior riding
  existing steps/flags), `discovery.yml` (weekly), `backfill-discovery.yml`,
  `ops.yml` (exclude/unexclude by slug), `resolve-website-fallback.yml` (husk
  re-mining dry-run/backfill lever, dry-run default), `eval-record.yml` (live
  golden-set re-recording → pushes a branch; repo settings forbid
  Actions-created PRs).
- **Concurrency displacement:** DB-writing workflows share one concurrency
  group; GitHub keeps only the newest PENDING run — queued dispatches
  displace each other and the cron. Batch loops must re-dispatch on
  `cancelled` and should wait for an empty queue between dispatches or they
  starve the cron (this happened; see worklog "drain v4").
- The user's Mac disk runs near-full; prune `.claude/worktrees/` and
  node_modules/.next copies after agents finish.

## Autonomous processes currently running (no babysitting required)

- The 3-hourly pipeline cron: news/funding, `resolve-website-fallback --limit 25`
  (husk re-mining, NEW #174 — drains ~25 website-less husks/run before
  resolve-homepages), scrape/enrich (+ husk rescue priority),
  `embed-companies --limit 200` (embed backlog drains ~1–2 days from
  2026-07-12), redescribe tail, judge, then the read-only reports (db-stats,
  `data-quality` NEW #175, pipeline-health) → Actions step summary.
- Weekly discovery cron: VC portfolios, GitHub trending, dedup, competitors,
  `compute-themes` (TTL-gated monthly — the FIRST themes run happens on the
  next weekly run after embeddings exist).
- Both one-time prod drains are COMPLETE: non-US exclusions (runbook lever 1,
  drained to empty selection) and the W-F re-description backlog (~1.7k+
  profiles regenerated; gate = two consecutive zero-write batches).

## Verification commands

pipeline/: `uv sync && uv run ruff check . && uv run mypy src && uv run
pytest -q` (golden gate included; `uv run nous eval-prompts` for the metric
table). web/: `npm ci && npm run lint && npm run test && npm run build &&
npm run check:bundle && npm run test:e2e` (e2e structural block passes
secret-free — that's the CI contract).

## Open items, in priority order

The **ROADMAP "Now" horizon — data quality** is now substantially **COMPLETE**
(#1–#4 shipped; #5's internal primitive shipped). Remaining Now follow-ups are
small (below). The frontier is now the **NEXT horizon (depth)** — see `ROADMAP.md`.

1. ~~**Husk website re-mining**~~ — **SHIPPED (#172/#173/#174).** Live in the cron; drains ~25/run.
2. ~~**Data-quality dashboard**~~ — **SHIPPED (#175).** Read-only `data-quality` cron report.
3. ~~**Field normalization**~~ — **SHIPPED (#176/#177).** `hq_state`→USPS code (+ `normalize-hq-state` backfill), `formatUsd` exact-$ tooltips, thin-tag `noindex`.
4. ~~**Re-enable "report incorrect data"**~~ — **SHIPPED (#177).** Per-company `repoIssueUrl` rider on `/c/[slug]`.
5. ~~**Per-company completeness score**~~ — **internal primitive SHIPPED (#175).**

**Small Now follow-ups (do opportunistically):**
- Wire `util.completeness` into husk-enrichment prioritisation ordering; fold in
  `extraction_confidence`; expose a public trust badge (Later — provenance UI).
- Watch the `data-quality` cron report — esp. the website-provenance breakdown /
  wrong-site proxy for the husk re-mining (the Apex-class residual).

The frontier is now the **NEXT horizon (depth)**, detailed just below.

**NEXT horizon (depth, after the foundation):** the **market map** (#179/#180),
**momentum signals** (#181/#182), and **per-entity RSS** (#183) SHIPPED (see the
top update blocks). Remaining Next bets: **talent-flow** from `people` (founder
previously-at, repeat founders, exec moves) and **investor depth** (co-investment
networks, portfolio momentum). Full detail in `ROADMAP.md`.

Deferred (unchanged): the structured-describe fallback ("A", with its three
required fixes — see the worklog), and anchoring the judge/funding golden
floors with `--update-baseline` after a live `eval-record` run.

## Key architecture pointers

- Enrichment: `pipeline/src/nous/pipeline/enrich_companies.py` (two-call
  judge/describe flow, stamping semantics documented inline).
- Eval harness: `pipeline/src/nous/evals/` + `pipeline/tests/golden/README.md`
  (edit prompt → re-record live → review deltas → commit).
- Embeddings: stage `embed_companies.py`; RPCs `similar_companies` (0033) and
  `semantic_companies` (0035); web query embedder `web/lib/embed-query.ts`
  (CLS pooling parity is load-bearing).
- Themes: `compute_themes.py` (KMeans, centroid slug-stability, TTL gate).
- Web data layer: `web/lib/queries.ts` (supabaseOrNull pattern: benign
  degrade off-Vercel, loud `SupabaseConfigError` on Vercel).
- Runbook for exclusion sweeps: `docs/runbooks/non-us-and-nonstartup-backfill.md`.
