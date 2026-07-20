# Backlog

> **Strategic layer:** [`ROADMAP.md`](ROADMAP.md) holds the *why / what order*
> (Now / Next / Later bets); this file is the tactical *what next* queue. A
> roadmap bet becoming concrete work lands here as an entry.

> **2026-07-12 status sweep:** the `fable5/*` series (PRs #131–#155, see
> `docs/superpowers/fable5-worklog.md`) shipped large parts of this backlog:
> all P2 pipeline cleanups, the frontend fixes, slug aliases + 301s (Wave 2),
> and the Wave 3 embeddings stack (embeddings infra, similar-companies,
> semantic search, themes). Entries below are annotated SHIPPED where done;
> unannotated entries remain open.

The grind queue. Refreshed 2026-06-11 after a full codebase review + product
brainstorm: items shipped in PRs #23 and #28–31 were closed (the M5 P1 fixes,
index search/filters/pagination, `/about`, employee rendering, low-confidence
funding flags), and the product backlog below was added. Add new entries at the
bottom of the appropriate section; close items by deleting them.

**Severity / effort legend:**
- **P0** — correctness or cost risk; do before new features
- **P1** — should fix soon; **P2** — fix opportunistically
- **[S]** hours · **[M]** days · **[L]** a week or more

---

## 2026-07-17 post-surgery QA sweep (3 lanes vs prod) — the NEXT queue

Ran after the #216–#229 arcs (dedup, purge, refetch, platform health). The
garbage classes are verifiably reduced (healed checklist: helix PASS, away/
amiato PASS as clean husks, aardvark PASS on garbage) and browse/search took
zero P0s under adversarial probing. The dominant unfixed class is one level
deeper than aardvark: **same-name different-entity attribution**, which the
mention guard passes BY CONSTRUCTION (the article really does say "Wonder").
Full lane reports in the session transcript.

### P0 — name-collision entity resolution at round ingestion [L]
3 of 12 /trends "Biggest recent rounds" are wrong: bespoke-labs carries
IM8's $1B (source never says Bespoke), edtech-Wonder carries food-Wonder's
$650M, prometheus double-counts $10B+$12B of the same event. Also: wave
($2.4B total, ~94% is Primary Wave music / Third Wave forklifts), impulse
(+$294M from Impulse Dynamics), terrafirma (profile = TerraFirma Robotics,
round = TerraFirma Inc; source says Series A=$100M within $115M total),
genesis-therapeutics (a16z round on a telehealth practice profile), uala
(CHIMERA: Italian beauty marketplace identity + Argentine neobank money,
double-counted to $1.2B), sambanova ($100M KuCoin garble REGREW post-repair
— recurrence, not residue). Design direction (probe-first, husk-style):
entity-aware attachment — match the funded company's website/description
context against source text, LLM company-match adjudication on ambiguous
names; a retroactive entity-audit pass over high-prominence rounds; and a
surgical ops lever (no way today to delete ONE wrong round without excluding
the whole company). Recurrence-proofing is part of the bar.

**Progress (2026-07-18):**
- ~~surgical ops lever~~ — SHIPPED (#230 delete-round; #231 adds
  --clear-total/--clear-status for out-of-purge-set residuals + status-✓
  purge). NB: purged recent-news rounds RE-INGEST within hours via the 3h
  cron (wonder + terrafirma both recurred) — re-heals are deferred until
  the ingest guard lands.
- ~~$0 probe~~ — SHIPPED (#232 audit-round-entities; #233/#234 calibrated
  against 3 live prod dispatches: 706→213 suspects of 1112 checked). The
  run-3 report IS the retroactive-audit candidate set; headline finds:
  built←"Built In" $30B (outlet-name collision), blue←Blue Origin $10B,
  magic←Magic Leap/Eden/Spoon (3 entities), adaptive←Adaptive Security,
  clipboard←Clipboard Health, odyssey/maze/amber/fathom←Therapeutics,
  drip←Drip Capital, bunkerhill←Bunkerhill Health (the dedup-miss pair),
  prometheus $6.2B = "Project Prometheus" (same-entity dedup case).
- ~~ingest-time guard~~ — SHIPPED (#235; validated live: 35 adjudications
  / 30 wrong-entity drops / 0 errors across the first three guarded runs,
  incl. all five food-Wonder GN re-ingests).
- ~~per-company retroactive purge lever~~ — SHIPPED (#237 + #238
  force-adjudicate default): the guard's decision over every STORED
  article of one company (extract-funding re-mines rounds from pre-guard
  articles — wonder re-spawned twice before this existed). Applied:
  wonder 11 articles + $650M round; terrafirma 9 + $100M round.
- NEXT: golden set for article_subject_match (eval-record live), then the
  retroactive audit — dispatch purge-wrong-entity-articles-dry-run per
  probe suspect (213 list, run 29642507263; built/blue/magic first),
  review verdicts, apply. prometheus $6.2B routes to dedup widening.

### ~~P0 — dedup signal gaps the sweep proved~~ — SHIPPED (#240)
All four: continuation-suffix normalization (uala), investor-evidenced
widened near-amount band 15–25% (prometheus), equal-valuation
cross-amount pass (sambanova), and the bunkerhill root cause (both
websites NULL; the LLM gate never saw the shared $55M round — the
company-match prompt now carries latest-funding evidence). Verify: the
next 3h cron's repair counters + the weekly dedup merging the
bunkerhill pair.

### P1 — contained wrongness + web silent mismatches
- harbor: ~50 keyword-garbage news rows — "harbor" absent from the curated
  _COMMON_NAME_WORDS (~70 words); replace/augment the list with a
  frequency-derived common-word test so coverage isn't list-bound. [M]
- callsign: pre-fix wrong-website residual (ham-radio profile + Accel
  investor + "Well documented" badge, broken #sources anchor) — repair
  pass coverage gap; also anthropic "Employees 10–50" (theorg) — suppress
  employee estimates wildly inconsistent with funding scale. [S–M]
- Investor identity splits: Nvidia vs "Nvidia Corp.", Bezos vs "Jeff
  Bezos", JPMorgan vs "JPMorgan Chase & Co.", QIA spelled out — round rows
  list backers twice; extend dedup-investors canonicalization. [M]
- ~~Timeline standalone-news firehose~~ — SHIPPED (#239): standalone
  articles cluster into stories (title-similarity + 7d window + money-
  mention veto) and reuse the round-coverage collapsed disclosure.
  Verify kalshi/baseten/crusoe/blue-origin timelines after ISR.
- /companies export vs page count under q=: page total includes semantic
  matches, /api/export is lexical-only (30 shown → 1 exported). Export the
  same blend or label the export. [S]
- stage= filter is case-sensitive and silently ignores mismatches (seed →
  all 2,326 unfiltered); normalize + surface unknown-value state. [S]

### P2 — smaller
- Completeness badge gates on document count, not entity confidence —
  wrong-entity pages wear "Well documented" (PRODUCT CALL: gate badges on
  entity-audit state once the P0 ships).
- Scope creep: biotech #3 industry, Blue Origin/Synchron/Science Corp in a
  "US software startups" catalog — eligibility sweep or reframe copy.
- Future-dated RSS pubDate (apaluma, +4 days); /watchlist and /c/* 404
  carry the generic root <title>; healthcare "+671%" tops Hottest off a
  $39M base (coverage-age gate for the ranking, not just the caveat);
  Kleiner "Backs 121" vs momentum "122"; "Together Ai" casing; compare
  drops the 5th slug silently; garbage semantic queries return 30
  confident results (relevance floor / soft empty state).

## 2026-07-17 embedder/Vercel decoupling — CLOSED (status quo, owner-decided)

Decision record (don't re-litigate): the /companies embedder STAYS in the
Vercel function. Rationale: #228's size gate + the CUDA-postinstall fix
(~406MB→~105MB deployed) + Vercel's Large Functions beta (5GB, auto-enroll)
retire the E-2 outage class; every offload either breaks embedding-space
parity (Supabase Edge Fns: gte-small/mean-pool → full re-embed) or adds a
new vendor (Cloudflare Workers AI). **Escape hatch if ever needed:** CF
Workers AI `@cf/baai/bge-small-en-v1.5` with `pooling:"cls"` (free tier
~1000x our query volume) — REQUIRES a ~50-text cosine-parity spike vs stored
fastembed vectors (bar: ≥0.99, rankings preserved) before any cutover.
Triggers to revisit: Large Functions GA terms regress, or webpack lock-in
blocks a needed Next.js upgrade.

## 2026-07-15 source-verification ("✓ Verified against source") — SHIPPED (#197–#201)

The owner-approved DeepSeek enhancement to the provenance moat (ROADMAP Later #1):
discriminatively verify each rendered fact against its cited source; show a ✓ for
`supported` verdicts only. **Complete + live** — probe/gate (#197), schema 0043
(#198), apply path + golden gate (#199), web ✓ (#200), live re-record (#200: 0.889
verdict accuracy, grounding_min 1.0 = zero fabrication). Prod holds 18 grounded
verdicts; widen with `verify-sources.yml -f run_apply=true -f limit=N` (idempotent).

**Remaining follow-ups:**
- ~~**Re-fetch path [M]**~~ — **SHIPPED (#223)**: `verify-sources --refetch`
  widens selection to the ~103 refetch-bucket facts; one polite live fetch per
  fact via `NewsClient.fetch_article_body` (robots, contact-email UA, 1 req/s,
  SSRF), text transient — never persisted. Opt-in (CLI flag + verify-sources.yml
  `refetch` input); cron untouched. Rollout: refetch dry-run dispatched
  2026-07-17 → review verdicts → apply in bounded batches (~$0.04 total).
- ~~**`unsupported` in the data-quality report [S]**~~ — **SHIPPED (#204)**:
  verdict counts + itemized unsupported facts in the cron report.
- ~~**Apply cron cadence [S]**~~ — **SHIPPED (#205)**: `verify-sources --limit 40`
  in the 3h cron (no new input; drains the backlog remainder too).
- **Claim-drift gap closed (#202)** — stale-claim sweep (pipeline) + grammar-
  anchored claim guard (web); a corrected figure can no longer keep a stale ✓.
- **Ellipsis-aware grounding (#208)** — PROMPT_VERSION 2026-07-16.1; legit
  "..."-elided quotes now ground (fail-closed); bump re-verifies the cohort
  (~$0.30) via the cron step.

## 2026-07-16 fresh customer-perspective QA (3 lanes vs prod) — triage

Full lane reports in the session transcript; quick wins shipped same-day
(#212 web polish, #213 portfolio_count cohort). What remains, by priority:

### ~~P0 — corrupted merged-entity records~~ — root-caused + repair SHIPPED (#215)
NOT dedup merges: article-URLs-as-homepages (helix→machinebrief,
away→marketspy, amiato→failory) — wrong-site descriptions + rounds mined off
the news site. Pass (e) detected the class all along but the repair was
never dispatched; it now runs every 3h cron WITH same-host round/article
purge (double-confirmed only), and the three hosts are blocklisted.
improbable excluded via ops (wrong entity + UK). **Residual to watch:** helix
rounds whose primary_news_url is on a third-party syndicator (aithority)
survive the same-host purge — the news-attribution arc (below) owns those;
check helix on the data-quality/unsupported report after re-enrichment.

<!-- original finding, kept for context -->
### (was) P0 — corrupted merged-entity records poison /trends [M]
QA H1/H2: `helix-digital-infrastructure` carries ANOTHER company's description
("Machine Brief is an AI news...") and four mis-merged rounds incl. a **$10B
KKR/Nvidia round** that single-handedly crowns media-entertainment the
top-funded industry on /trends and /industry. Same wrong-description class on
the media-entertainment page: "Away"→Market Spy, "Amiato"→Failory,
"Improbable"→Ig Nobel. Likely dedup false-merges or enrichment writing to the
wrong row. Investigate via `nous inspect-company` (ops.yml), identify the
merge/enrich bug, repair rows (repair-catalog pass or targeted ops), add a
regression guard. THE top trust item.

### P0 — aggregation-without-dedup on marquee pages [M] — probe + cron SHIPPED, merge gate next
QA: terrafirma total double-counts one Series A reported at $115M and $100M
(compatible types, amounts differ → reconcile misses; repair-duplicate-rounds
only merges EQUAL amounts). sambanova renders the same Series F ~8×
(near-identical Google News URLs beat the canonical-URL dedup) and its named
rounds don't reach the $4.1B total. blue-origin repeats one $10B event ~12×.
Prod inspection (2026-07-17, ops.yml): sambanova = 9 rounds for one event
(Series F dated + E/D/"Series ?" all $1B + 3 empty shells + KuCoin's garbled
$100M); blue-origin = 12 rounds, 10 signal-free shells from pre-rumor-guard
"seeks $10B" articles.
- ✅ **Shipped (P0a):** "suspect duplicate rounds" census in the data-quality
  report (empty shells / exact-dup losers / near-amount pairs ±15% /
  type-conflict groups — same compatibility rules the repair clusters with);
  placeholder round_types ("Series ?", "unknown") normalize to None for
  clustering; repair-duplicate-rounds promoted to EVERY 3h cron in apply mode
  (kills the shell backlog + exact dups; the #215 repair-wrong-websites pattern).
- ✅ **Shipped (P0b):** repair Pass 2b — near-amount merge (compatible types
  + compatible dates + amounts within ±15% of the anchor, greedy no-chain,
  survivor keeps its OWN sourced amount) — and Pass 2c — contradicting series
  letters fold into the single dated+typed anchor ONLY on stored
  publication-date evidence (±14d). Reconcile hardened at the source:
  placeholder types ("Series ?") never persist and never block a merge.
- ✅ **Shipped (P0c):** GN headline-variant dedup — ingest skips a
  headline-only Google-News fallback whose (company, title) already exists
  under another opaque CBMi… URL; repair-catalog pass 5 drains the stored
  backlog (survivor prefers round-linked > dated > oldest; publisher-URL rows
  and other companies' identical titles untouched).

### ~~P1 — wrong-entity news attribution (aardvark class)~~ — SHIPPED end-to-end (#219–#222, purge APPLIED 2026-07-17)
QA: `/c/aardvark` timeline is keyword-scrape garbage (Arthur cartoon, rugby
memorial, day-care funding) and its $85M Series C cites only a Google News
aggregator URL. ingest-news company matching needs a name-ambiguity guard
(generic dictionary-word names demand a stronger entity match), and the
existing news mis-attribution guard (#116) needs a second pass.
- ✅ **Shipped (guard + heal):** single-common-word names ("Away", "Clear")
  now require funding-subject context (funding verb within 2 tokens after /
  company marker before / appositive shape) — a bare tokenized word match
  ("diversify away from China") never attributes; cloudflareaccess.com hosts
  + /cdn-cgi/ paths join the reject set (resolver + repair pass (a) — heals
  away's stored JWT login URL on the next cron); the wrong-company reset now
  clears people/competitors/industry/HQ/embedding, and new pass (f) drains
  the pre-fix residue on helix/amiato/away (fixes the /trends
  media-entertainment $10B misfile — the round is real, the industry wasn't).
- ✅ **Shipped (purge, #220/#221/#222):** repair-misattributed-news re-runs
  the hardened guard over every stored article; ops.yml dry-run/apply lever;
  batch-loaded scan (the per-company loop blew ops' 10-min timeout); two
  precision spares from the prod dry-run review (squashed name, distinctive
  head token — dictionary heads denied). **APPLIED on prod 2026-07-17:
  2,861 articles + 35 wrong-entity rounds deleted across 577 companies**
  (helix's Kinoa/Coval/ChatSee rounds + aardvark/away timeline garbage gone;
  dry-run and apply counters matched exactly). Deleted-but-genuine articles
  self-heal: the hardened ingest guard re-admits them from the next news
  cycle.

### ~~P1 — "in talks" rumor language verified as a completed round~~ — SHIPPED (#214)
Both layers hardened (funding_extraction 2026-07-16.1 + source_verification
2026-07-16.2), live-re-recorded (verdict_accuracy 0.888→0.947, the new
in-talks case verifies `unsupported`); the cron's version-gated re-verify
strips existing rumor ✓s. **Deferred follow-ups [S]:** a clarifying
parenthetical on the valuation rule (it says "Always capture" while the rumor
rule nulls it — works live, latent ambiguity) + a mixed golden case (source
with a COMPLETED $50M and "in talks for more" → claim about the $50M =
supported). Batch both with the next eval-record run.

### P2 — smaller QA items
- ~~**/trends backfill artifact framing [S]**~~ — SHIPPED: a coverage-honesty
  line on /trends (growth vs pre-coverage windows is coverage-relative).
- **Blue Origin in a "US software startups" catalog:** judge-eligibility
  scope leak — re-judge; also "Jeff Bezos"/"Bezos" investor dup.
- **Google News URLs as visible sources:** resolve-at-ingest missed a cohort;
  consider a bounded re-resolve backfill for primary_news_url redirects.
- **/new missing descriptions** (husk cards — heals as enrichment covers
  them) [S].
- **pipeline_runs.finished_at index [XS]:** /stats orders by finished_at DESC
  with only a stage index present — fine at ~40 rows/day for years; add
  ix_pipeline_runs_finished_at with the next migration touching that area.
- **uncertain/unsupported boundary is temperature-sensitive [S]:** back-to-back
  live re-records flipped 3 silent-source cases uncertain→unsupported (never a
  ✓ risk — both are badge-less — but it noises the internal unsupported
  signal). A future source_verification hardening pass should sharpen the
  silent-vs-contradicts wording; until then, review re-record deltas
  case-by-case before committing recordings. ~~Future-dated entries~~ — SHIPPED: bucket headings carry an
  explicit UTC tag.
- ~~**/vs + /alternatives sitemap policy**~~ — DECIDED, no code: /alternatives
  is already in the core shard (#209); /vs pairs stay crawl-discovered via
  company-page links (enumerating indexable pairs needs a competitor-edge
  query for marginal SEO value — revisit only if /vs impressions matter).
- ~~**404 server-rendered `<title>`**~~ — DECIDED, no code: Next streams the
  not-found title after the initial head (framework behavior); the 404 status
  code is correct so indexing impact is nil.
- **VerifiedBadge hover quote** reported absent in served HTML — verify
  whether the title attribute survives streaming; may be a QA-tooling
  artifact [S].

## 2026-06-16 product review + remediation — SHIPPED

Review: [2026-06-16-product-review-and-next-steps.md](docs/superpowers/plans/2026-06-16-product-review-and-next-steps.md).
Execution log + activation steps: [2026-06-16-remediation-execution-log.md](docs/superpowers/plans/2026-06-16-remediation-execution-log.md).

Every bug the four-persona review found, plus the high-value backlog items below,
shipped as PRs #112–#128 (verified on prod):

- ✅ Husk notice + `discovered_via` label (#112) · marquee-husk enrichment
  prioritisation (#114) · wrong-company profile detect + resolver hardening (#117)
  · funding sources → publisher URLs (#118) · phantom valuation rounds (#124)
- ✅ Eligibility rejects non-startups + opt-in re-judge (#115) · news
  mis-attribution guard (#116)
- ✅ Investor dedup a16z/junk/angels (#113) · compare selection UI (#119) ·
  investor pagination + profile (#120) · amount tooltips + attribution (#121)
- ✅ Company logos (#122/#125/#126) · name-quality casing (#123) · state-display
  normalization (#125) · Alternatives pages + FAQ JSON-LD (#126)
- ✅ Adapter-health canary (#127) · filter-column indexes / migration 0030 (#128)
- ✅ Stale `repoIssueUrl` comment removed (#112-era)

**Pending activation** (one-time prod dispatches + workflow wiring — see the
execution log's "Activation" section): run `repair-wrong-websites` /
`repair-duplicate-rounds` over existing rows; `judge-eligibility
--rejudge-nonstartup-signals` for the Manta/Lucra leaks; wire `name-quality` +
`adapter-health` into `discovery.yml`. The every-3h cron heals going-forward data
automatically.

**Still open from the review:** news-list de-dup/ranking (E2); a deliberate non-US
backfill drain (V2 — eligibility now rejects on entry, but existing foreign rows
like Mistral/Clio need a sweep); mobile-responsiveness pass (P3).

---

## 2026-07-13 ROADMAP "Now" horizon — data-quality foundation

Strategic context: [ROADMAP.md](ROADMAP.md) (Now horizon). Earn the right to be
trusted before building depth. Sequence: measure quality → fix the biggest hole
(husks) by re-mining not re-scraping → make correctness visible. New items are
detailed below; existing open entries pulled into this push are cross-referenced
at the end.

### Resolve husk websites by re-mining, not re-scraping [M] — P1 — SHIPPED (#172/#173/#174)
New idempotent `resolve-website-fallback` stage resolves website-less husks from
non-origin sources, first accepted candidate wins, `$0`, self-bounding on
`website IS NULL` + its own `website_fallback_checked_at` stamp, wired into the
3h cron before resolve-homepages (drains ~25/run). Provenance recorded per
resolved site (`website_source` + `website_source_url`, migration 0037).
- **wikidata** — Wikidata "official website" (P856) for a name + org-type +
  country matched entity (three precision gates; a conservative country
  cross-check rejects confirmed-foreign same-name collisions). **Highest yield +
  precision.**
- **news_outbound** — the company's homepage link in an already-sourced news
  article body, re-fetching the *article* (not the origin) and matching by
  domain-label / anchor name.
- **Dry run (30 prominent husks):** 11 resolved (37%), 0 conflicts, ~10/11
  correct, `$0`. wikidata 9, news_outbound 2 (disjoint).
- **Not built:** VC-portfolio source (the roadmap assumed `raw_pages` caches
  portfolio pages — it doesn't; it's company-scoped, and portfolio adapters
  already capture `entry.website` at discovery, so it's redundant for
  portfolio-discovered husks). Common Crawl (weak for name→domain). Revisit only
  if the dashboard shows the residual husk count stays high.
- **Follow-up:** the faster-backfill lever (`resolve-website-fallback.yml`
  dispatch, `dry_run=false`) can drain the ~890 backlog quicker than 25/run if
  the gradual cron drain proves too slow.

### Data-quality dashboard [M] — P1 — SHIPPED (#175)
Read-only `data-quality` stage (completeness sibling of db-stats/pipeline-health)
emits a step-summary report over the shown cohort: field-completeness %s
(website / description / funding / logo / people / location / industry / tags /
employees), **website provenance by `website_source`** (surfaces the #174
re-mining contribution + wrong-site proxy), completeness-score distribution,
duplicate rate (shared `normalized_name`), enrichment staleness. Id-free cron
step. **Follow-up:** a web-facing version is ROADMAP Later (provenance UI); this
is internal-report-only for now.

### Per-company completeness / confidence score [S] — P2 — SHIPPED (internal primitive #175; stored for web #191; badge #192)
Pure `util.completeness` weighted 0..1 score (husk-defining fields dominate),
aggregated by the data-quality report. **#191 stored it** on
`companies.completeness_score` (migration 0042 + the `compute-completeness`
stage) so the web renders the badge without re-implementing the scorer in TS;
**#192 shipped the public trust badge** — the positive-only "Richly/Well
documented" badge in the `/c/[slug]` "Data & provenance" panel (PRs 1–2 of 3 of
the provenance UI, ROADMAP Later #1). **Remaining:** wire the score into
husk-enrichment prioritisation ordering; fold in `extraction_confidence`
(field-presence only for now — PR 3 surfaces the enum as a tooltip).

### Pulled into this push — existing open entries
Consciously scoped into the Now horizon; tracked in their home sections, listed
here so the push is complete:
- **"Report incorrect data" link** (Wave 1) — **SHIPPED (#177)**: per-company
  `repoIssueUrl` rider restored on `web/app/c/[slug]/page.tsx` (repo public → the
  prefilled GitHub-issue link resolves).
- **`formatUsd` rounding collapses distinct amounts** — **SHIPPED (#177)**:
  `title={formatUsdExact(amount)}` on every individual funding figure.
- **`hq_state` unnormalized (CA vs California)** — **SHIPPED (#176)** —
  canonicalized to the 2-letter USPS code at enrichment write-time + a
  `normalize-hq-state` backfill.
- **Tag min-companies threshold** — **SHIPPED (#177)**: `/tag/[tag]` noindex when
  <3 companies, in lockstep with the sitemap's existing ≥3 filter.

---

## 2026-07-13 ROADMAP "Next" horizon — depth features

Strategic context: [ROADMAP.md](ROADMAP.md) (Next horizon). Depth pros return
for, built on top of the clean data foundation.

### Per-entity RSS feeds [S] — SHIPPED (#183)
The global `/feed.xml` firehose fanned out to three per-entity scopes — "watch
this" without accounts, `$0`, works immediately against existing data:
- **`/c/[slug]/feed.xml`** — one company's funding rounds + news. Reuses the
  `/c/[slug]` timeline query (`getCompanyBySlug` already returns both) rather
  than a new query; 404s on an unknown/excluded company.
- **`/industry/[group]/feed.xml`** — funding + news across a canonical
  `industry_group`. Slug hard-gated via `resolveIndustrySlug` (same gate as the
  page); non-canonical slug → 404.
- **`/investor/[slug]/feed.xml`** — funding + news across an investor's resolved
  portfolio companies (both link paths, excluded companies dropped); slug set
  capped (`FEED_IN_SLUGS_CAP=150`) to bound the request URL.
- **Shared layer** (`lib/rss-items.ts`): row→`RssItem` mappers with the stable
  `funding:`/`news:` guid scheme, newest-first merge, and the cached RSS
  `Response` — the global `/feed.xml` route was refactored onto it too, so all
  four feeds emit an identical item shape. New scoped queries
  (`listRecentFundings/NewsByIndustry`, `listRecentFundings/NewsForCompanySlugs`)
  mirror the global ones + one scoping filter, shown-cohort only.
- **Discovery:** each entity page's `generateMetadata` emits a
  `<link rel="alternate" type="application/rss+xml">` for its feed, plus a
  subtle visible "Follow via RSS" link near the header (`components/RssLink.tsx`).
- Every feed degrades to empty-but-valid on missing Supabase (never 404/500);
  guids stable across regenerations. Follow-up: an on-page "how to subscribe"
  hint / feed hub, and email delivery (explicitly out this quarter).

### Talent-flow "founder background" rider [M] — SHIPPED (#185/#186/#187/#188, recordings #189)
Per-company "founder background / notable alumni" rider via a bounded DeepSeek
career-history extraction, built on the thin #184 signal (rider not graph). The
$0.05 dry run cleared the gate (#185: 50% of top-funded yield ≥1 named prior, **0
fabrication**), then migration 0040 `career_moves` (#186), the version-gated +
idempotent apply stage + golden set (#187, migration 0041 stamp so empties aren't
re-billed), and the `/c/[slug]` web rider (#188). Live golden re-record #189
(grounding 1.0). Cost: **~$0.0013/company** (measured) → full backfill well under
the ~$6.50 estimate. Design: `docs/superpowers/plans/2026-07-13-talent-flow-rider-and-investor-depth.md`.
Follow-ups (BACKLOG, low-priority): a catalog-level "repeat founders" index
(co-membership by `person_normalized_name`, $0 but low-precision — no person
disambiguator); re-extract as scrape coverage grows (prompt bump re-selects).

### Investor depth [M] — SHIPPED (co-investment pre-existing; portfolio momentum #190)
Turned the investor directory from a list into a lens, $0 / no LLM, from existing
linkage. **Co-investment** ("frequently co-invests with") already shipped
(`getCoInvestors`, read-time, capped). **Portfolio momentum** (#190): aggregate
`momentum_score` (#181) across an investor's portfolio → "N of M heating up" +
the hottest few, on `/investor/[slug]`. Remaining follow-ups (unstarted, P2):
- **"Who's leading rounds in industry X right now"** — recent rounds by
  `industry_group` + their `is_lead` investors, on `/industry/[group]`. $0, from
  `funding_round_investors` + `funding_rounds` + `companies`.
- A **global co-investment meta-graph** (investor↔investor network view) — would
  need a materialized edge table if it goes beyond a single investor's page
  (per-investor is O(N) read-time; all-pairs is O(N²) — persist if pursued).

---

## Pipeline cleanups (P2)

### Throttle/get helper triplicated across source clients [M]
**SHIPPED — PR #132.**
[homepage.py](pipeline/src/nous/sources/homepage.py),
[news.py](pipeline/src/nous/sources/news.py), and
[headless_browser.py](pipeline/src/nous/sources/headless_browser.py) each
reimplement domain locks + throttled GET + tenacity. They also keep separate
lock dicts, so HomepageClient and HeadlessBrowserClient do **not** actually
cooperate on the 1 req/sec/domain budget despite the comment claiming they do.
Extract a `ThrottledHTTPClient` in `sources/_http.py` with a shared registry.

### Add btree index on `companies.hq_state`
**SHIPPED — PR #128 (pre-series).** and GIN on `companies.tags` (now in `WHERE` via /location and /tag pages); batch with other unindexed filter columns (`industry_group`, `discovered_via`). [S]

---

## Frontend fixes (P2)

### Description-source attribution is misleading [S]
**SHIPPED — PR #121 (pre-series).**
[c/[slug]/page.tsx](web/app/c/%5Bslug%5D/page.tsx) says "generated by … from
[hostname]" even when the description was derived from multiple pages. Soften
to "Generated on [date]" or track per-description sources.

### Missing Supabase env collapses into 404 [S]
**SHIPPED — PR #138.**
[queries.ts](web/lib/queries.ts) returns `null`/empty indistinguishably for
"missing env" vs "no row", so a misconfigured deployment 404s every page.
Throw at module load (server-only) so misconfigs fail fast and loud.

### Total-raised tile may double-count overlapping rounds [S]
**SHIPPED — PR #138.**
The detail page sums `amount_raised` across all rounds; if
`reconcile_funding_round` ever fails to merge two articles about the same round,
the tile double-counts. Document the assumption near the sum; longer-term add a
`round_correction_of` pointer for amended rounds. Since the hybrid total-raised
change, an article-stated cumulative total caps the displayed figure whenever
articles state one that exceeds the sum (the tile shows max(stated, sum) —
partial mitigation); the reconcile-merge risk itself stands.

### ~~`formatUsd` rounding collapses distinct amounts~~ [S] — SHIPPED (#177)
$1.51M and $1.49M both rendered as "$1.5M"; now every individual funding figure
carries a `title={formatUsdExact(amount)}` exact-dollars tooltip.

### ~~`hq_state` values are unnormalized (CA vs California) — location pages render stored casing; normalize at enrichment time.~~ [S] SHIPPED (#176)
Canonical form = the 2-letter UPPERCASE USPS code (the form the `/location/[state]` route already matches on — routing-safe). Applied at the enrich-companies write site via `canonical_us_state` (`util/us_state.py`, 50 states + DC; non-US → None → left untouched) plus the bounded, idempotent `normalize-hq-state` backfill stage (`--limit` / `--dry-run`, self-bounding SELECT, per-row commit). No migration (content-only), no URL change (full-name `/location/California` links 404 today and start resolving to the working `/location/CA`).

### ~~Tag sitemap min-companies threshold~~ [S] — SHIPPED (#177 noindex; #209 shards)
`/tag/[tag]` noindexes when <3 companies (#177); **#209 sharded the sitemap**
(`/sitemap/core.xml` + `/sitemap/companies-<i>.xml` at 40k/shard, robots.txt
lists every shard) so the catalog grows past the 50k-URL cap without rework.

### De-emphasized text/controls below WCAG AA contrast (`text-ink-faint`/`-muted`) [S] — SHIPPED (#195)
**#195 did the system-wide pass:** lifted `--ink-muted` to AA (#8a8a8a→#6d6d6d
light = 4.96:1; #5f5f5f→#808080 dark = 5.01:1) — fixing every readable
`text-ink-muted` site at once; reclassified the 31 readable `text-ink-faint` uses
→ `text-ink-muted` (leaving 15 WCAG-exempt: aria-hidden, disabled pagination, `—`
placeholders); and normalized disclosure focus rings (added `summary` to the
global `:focus-visible` outline; dropped the 40%-opacity custom rings from
`EventTimeline`/`FilterPanel`). **Remaining (minor, optional):** `--ink-faint`'s
value stays 1.4:1 but now only on decorative/disabled/`—` uses (exempt); the
brand `--accent` as link text is ~4.36:1 (marginally under 4.5) — a separate,
brand-loaded token change, deferred.

<!-- original writeup, kept for context:
Surfaced by the #193 + #194 reviews: `--ink-faint` (#d4d4d4 on the #fafafa light
canvas ≈ **1.42:1**; 1.82:1 dark) AND `--ink-muted` (#8a8a8a ≈ **3.3:1** light /
3.1:1 dark) are used pervasively (~30 places) for de-emphasized supplementary text
(footers, ranks, `app/page.tsx:253` "+N more", the source-type tags + host links
in `Sources`, the timeline coverage disclosure summary + article links in
`EventTimeline`) — below SC 1.4.3's 4.5:1 for text (`-muted` clears only the 3:1
large-text / non-text floor). #193 fixed the two trust-critical instances (the
source `↗` glyph → `text-ink-muted`). Also: the `EventTimeline` coverage
`<summary>` (and `FilterPanel`) use a 40%-opacity `focus-visible:ring-accent/40`,
fainter than the site-wide `outline: 2px solid var(--accent)` (which only targets
`a/button/input/[tabindex]`, not `summary`). Do a system-wide pass: audit
`text-ink-faint`/`-muted` sites, bump readable/interactive ones to a token ≥AA (or
lift the token values), and normalize disclosure focus rings to the global
standard. Token-level change — verify no regression in the intentionally-quiet spots.
-->

### ~~Mobile masthead nav overflows the viewport~~ [S] — SHIPPED (#196)
The primary nav rendered all 8 links at every width, so on phones the row
overflowed and the whole page scrolled horizontally (~90px at 570px, worse at
375px). Collapsed into a `MobileNav` `☰` client island below `lg` (shared
`PRIMARY_NAV`; desktop nav `hidden lg:block`); verified 0px overflow at 375px.

### ~~`news.google.com` citations render untagged in Sources~~ [S] — SHIPPED (#196)
It's the host behind most funding citations but was missing from `NEWS_HOSTS`, so
only the "Website" self-citation carried a source-type tag. Added the exact host
(not bare `google.com`) → "News" (Google News only indexes news, never a mislabel).

### Populate prod `completeness_score` so the provenance badge renders [XS] — P2 — OPS, not code
A live QA pass found the "Richly/Well documented" badge on **0 of ~350** companies.
NOT a bug — `ProvenancePanel`/`completenessLabel` are correct; prod
`completeness_score` is simply unpopulated (the `compute-completeness` stage,
#191, runs on the weekly `discovery.yml`; it shipped 2026-07-14). Action: dispatch
`discovery.yml` (its TTL-gated `compute-completeness` step runs) or wait for the
Monday cron, then confirm the badge appears for high-score companies.

### ~~Coverage grouping degrades on undated funding rounds~~ [M] — SHIPPED (#206 pipeline / #207 web)
Migration **0044** `news_articles.funding_round_id` (FK SET NULL, self-healing
via repair-catalog pass 4 + repair-duplicate-rounds repointing); extract-funding
stamps the exact link at reconcile time; `buildTimeline` attaches by it first
(date proximity stays the fallback for legacy/unlinked articles). **Merge order:
#207 only after #206's migration reaches prod.** Historical non-primary articles
covering undated rounds remain heuristic until re-extraction.

### Provenance sourcing line slightly overstates on unsourced figures [XS] — P3 — owner copy call
`ProvenancePanel`'s "Every figure here links to a recorded source" shows whenever
≥1 citation renders, but an individual figure can lack a `↗` (e.g. a total-raised
with no `total_raised_source_url`, as on Milestone). Minor wording nuance; softening
risks the "never advertise a gap" moat framing, so left for the owner to decide.

---

## Product backlog — Wave 1: free wins

All buildable from data already in the DB; mostly frontend.

### "Report incorrect data" link [S]
Prefilled GitHub-issue URL on every company page. Crowdsourced QA, zero backend.
Built in PR (feat/seo-pack) but rendering deferred — repo is private so the
issues URL 404s for visitors. Re-enable the rider in web/app/c/[slug]/page.tsx
when the repo goes public (or swap target to a public form/mailto).

### Name-quality pass [S]
Prefer the company's own `og:site_name` / `<title>` casing (already in
`raw_pages`) over VC-portfolio casing. Folds in the old `name_quality`
source-priority idea: rank sources, overwrite only on higher quality.

### Logos via favicon fetch [S]
`companies.logo_url` exists and is mostly unused. Fetch
`/favicon.ico`/`apple-touch-icon` during scrape-homepages; render on cards and
detail header.

---

## Product backlog — Wave 2: the relationship graph (differentiator)

Build order matters: each step makes the next cheaper. Full design notes in the
2026-06-11 review.

### Fuzzy competitor linking [S–M]
[analyze_competitors.py](pipeline/src/nous/pipeline/analyze_competitors.py)
resolves competitor names by exact `normalized_name` only, leaving many edges
dangling. New `link-competitors` stage: pg_trgm `func.similarity` (the pattern
already in [dedup_companies.py](pipeline/src/nous/pipeline/dedup_companies.py))
≥ threshold, best-match-only with a tie guard, only touches NULL FKs. Zero LLM
cost; instantly densifies the graph. Call the same helper from
analyze_competitors at write time going forward.

### `company_relationships` edge table + derive stage [M]
Typed edges: `competitor | partner | vendor_of`, with `counterpart_name`,
`source`, `source_url`, evidence quote, confidence; unresolved counterparts kept
by name; unique on `(company_a_id, normalized_counterpart_name, rel_type,
source)`; canonical a<b ordering for symmetric types. Keep `competitors` as-is
(ranked per-company artifact with a UI contract) and project resolved pairs into
the edge table via a set-based `derive-relationships` stage (replace-style,
zero LLM). Do **not** materialize shared-investor edges (O(N²) with YC-scale
portfolios) — derive those at read time, capped.

### Related-companies module on `/c/[slug]` [M]
Server-rendered section grouping edges by type ("competes with", "works with")
with evidence/source links, plus an "also backed by" fallback via a two-hop
`company_investors` query (exclude investors with >30 holdings). First
user-visible payoff of the graph.

### "Alternatives to X" pages [M]
Generated from competitor edges. Huge search volume; makes the graph data earn
traffic before any visualization exists.

### "X vs Y" compare pages [M]
Competitor pairs define the URL space; render two profiles side by side.

### LLM partner/supply-chain extraction — dry-run first [M]
**Risk gate:** before building plumbing, run the extraction prompt over ~20
companies' existing articles/pages (~$0.50) and inspect yield + hallucination
rate. Funding news rarely names vendors and customer logos are images, so this
edge type may be sparse. If yield is good: `extract-relationships` stage over
already-cached `news_articles` + `raw_pages`, new prompt under `llm/prompts/`,
capped ~100 articles/run (~$0.15/wk), weekly cron in the shared concurrency
group. If poor: competitor edges + themes carry the map; drop the type.

### Market map — `/map/[industry]` [L] — SHIPPED (#179 pipeline, #180 web)
**Pipeline side SHIPPED (#179)** — `compute-map-positions` stage: per-industry
scikit-learn PCA(2) over the shown+embedded description embeddings (E-1),
deterministic sign-pin + per-axis min-max to `[0,1]²`, written to three new
nullable columns on `companies` (`map_x`, `map_y`, `map_computed_at`; migration
0038). Coords are comparable only *within* an `industry_group` (own PCA basis).
`$0` — local CPU PCA, no LLM, no network; reuses the `embeddings` uv group;
per-industry TTL-gated (25d) off weekly `discovery.yml` → effective monthly.
The web read is a flat single-table `WHERE industry_group = $1 AND map_x IS NOT
NULL` (no RPC, no PCA on Vercel — the #157 lesson).

**Web side SHIPPED (#180)** — shipped as a **static server-rendered SVG** (no
client component, no ML on the web function — the #157 lesson): `/map/[industry]`
reads `map_x`/`map_y` and renders nodes (SVG `<a>` links, funding-sized radius,
greedy non-overlapping labels, a11y via `aria-labelledby` + `sr-only` fallback)
plus a `/map` hub, both canonical-gated + coords-gated in the sitemap.
Migration-ordering-for-free: the queries degrade to an empty-state until coords
land. **Follow-ups (deferred):** an interactive client renderer (d3-force /
`react-force-graph-2d`) + theme coloring + a global theme-level meta-graph; one
visual tuning call (per-axis min-max exaggerates the lower-variance PC2 — switch
to a single shared scale factor to preserve the true PC1:PC2 ratio).

### `slug_aliases` table with 301 redirects [M]
**SHIPPED — PR #141 (308 miss-path redirects).**
Promoted from Future: dedup merges actively delete loser rows today, burning
inbound links and SEO equity. Keep old slug → 301 → new slug; middleware in
`web/` reads the table. Record aliases at merge time in
[dedup_companies.py](pipeline/src/nous/pipeline/dedup_companies.py).

---

## Product backlog — Wave 3: intelligence ("what's evolving")

### Embeddings infrastructure [M]
**SHIPPED — PR #153.**
pgvector (free on Supabase; `CREATE EXTENSION vector` in a migration) +
`companies.embedding vector(384)`. Generate with fastembed
(`BAAI/bge-small-en-v1.5`, ONNX, CPU) inside GitHub Actions — $0, seconds per
run; optional uv dependency group so the main install stays light; cache the
model dir. ~8MB storage at 5k companies; exact scan is fine, no index needed.

### Semantic search [M]
**SHIPPED — PR #155.**
"Startups doing AI for logistics" — embed the query, nearest-neighbor over
company embeddings, blend with the existing ilike search on the index page.

### Themes pipeline + pages [L]
**SHIPPED — PR #154.**
Monthly `compute-themes` stage: cluster embeddings within each `industry_group`
(KMeans/HDBSCAN), one DeepSeek call per cluster to name it (~50–100 calls =
pennies) → `themes` + `company_themes` tables (replace-style per industry;
centroid-match to previous run at cosine ≥0.9 to keep slugs stable-ish).
`/themes/[slug]`: member companies, funding-by-quarter (server-rendered SVG
bars from `funding_rounds.announced_date`), new entrants. `/themes` index
ranked by trailing-2-quarter funding growth — the literal "what's heating up"
page.

### Industry pages — `/industry/[group]` [M]
Company count, 12-mo funding velocity, median round, most active investors,
newest entrants, market-map embed.

### Trends dashboard — `/trends` [M]
Funding by industry over time, rising tags, heating/cooling indicators. All
derivable from `announced_date` + `created_at`.

### Similar-companies module [S]
**SHIPPED — PR #153.**
Nearest neighbors by embedding on every company page ("people also viewed"
without needing analytics). Rides on the embeddings infra.

---

## Product backlog — Wave 4: habit loop & breadth

### Weekly auto-digest page + RSS [M]
LLM writes a short "this week in startups" from the pipeline delta (new
companies, new rounds); published as a page + RSS feed. Keep it
aggregate-grounded — numbers from the DB, prose around them. Email is
deliberately deferred (first true cost item).

### Watchlists via localStorage [M]
"My companies" with new-round badges since last visit. No accounts, no backend.

### Momentum signals [M] — SHIPPED (#181 pipeline, #182 web)
**Pipeline (#181):** `compute-momentum` stage + migration 0039 score every shown
company's weekly "heating up" momentum into `companies.momentum_score` (`[0,1]`,
0.5=flat, NULL=insufficient data; partial DESC-indexed), `momentum_computed_at`,
`momentum_why` (pre-worded chips). Score = weight-renormalized mean over the
PRESENT of news acceleration (0.50, `company_snapshots.news_count_30d`
recent-vs-baseline, +K smoothed & `[¼,4]` clipped), funding recency (0.35,
`latest_round_date` exp-decay τ=180d), headcount growth (0.15). Anchored to
`as_of_week` (deterministic); weekly in `discovery.yml` after Snapshot companies
(not TTL-gated). $0. Launch reality: until ~6 weekly snapshots accrue, scores
are funding-recency-driven (news component ABSENT); self-enriches as history
builds.
**Web (#182):** `/trending` ("Heating up") ranked CompanyCard grid by
`momentum_score` desc + `🔥 Heating up` badge (`MOMENTUM_BADGE_THRESHOLD=0.65`) on
cards/company header + a pipeline-worded "why" line; nav/footer/sitemap. ISR,
migration-order-free (empty-state until scores land).
_Follow-ups (deferred):_ homepage "Heating up this week" strip (after the
"Trending now" naming-coherence call); badge-threshold calibration once the
score distribution is known; per-industry `/trending` scoping; a momentum
sparkline from `company_snapshots` history.

### `company_events` unified timeline [L]
Generalize funding extraction into event extraction: funding, acquisition,
launch, leadership change, layoffs — one timeline table, one timeline component
on the company page. Feeds the digest. Builds on the Wave-0 status detection.

### Startup of the day [S]
Deterministic daily pick (hash of date) from enriched companies; shareable.

### Compare view [S]
Side-by-side 2–3 companies (distinct from the SEO-oriented X-vs-Y pages:
user-selected, not pre-generated).

### Funding timeline SVG [S]
Small server-rendered visual above the funding table.

### Tech-stack detection [M]
Parse cached homepage HTML for stack hints (script srcs, meta generators) →
"built with" chips. New extraction over existing `raw_pages`, no new scraping.

### Discovery adapters [S each]
One `sources/` adapter apiece: PRNewswire/BusinessWire RSS (funding hits the
wires before TechCrunch), VentureBeat + GeekWire RSS, GitHub trending →
company mapping (devtools channel TC misses), accelerator demo-day lists.

### AI-answer-engine distribution [M]
`llms.txt`, a markdown endpoint per company (`/c/[slug].md`), FAQ block ("What
does X do? Who founded X? How much has X raised?") with FAQPage JSON-LD.
Getting cited by ChatGPT/Perplexity is the new SEO and our clean sourced data
is exactly what they want.

### `company_aliases` table [M]
Carried from Future: track every name variant seen per company + source.
Recovers from bad name choices with an audit trail; unlocks "you searched
'OpenAI Inc' → here's OpenAI" search behavior.

---

## Ops & quality hardening

### Adapter canary tests [S]
VC portfolio scrapers break silently on site redesigns. Weekly job asserts each
adapter yields > N entries; alert (issue) on collapse. Cheapest insurance
available.

### LLM eval golden set [M]
~20 hand-checked articles → expected extractions, run on every prompt change.
Prompt edits currently ship blind.

### Prompt versioning [S]
Stamp a prompt version on every extraction row so data produced by a bad prompt
revision can be selectively re-run.

### Pipeline observability [M]
`pipeline_runs` table (stage, started/finished, counts, errors); workflow opens
a GitHub issue on failure; public `/stats` freshness page (doubles as a trust
signal for readers).

### Sentry (free tier) for web; Lighthouse CI [S]

### Vitest + one Playwright smoke test for `web/` [M]
Zero web tests today; `npm run build` typechecks but misses render-time bugs.
One happy-path "/c/[slug] renders" test is high-leverage.

---

## Future ideas (need a spec discussion first)

### Human-review admin for dedup candidate pairs
`dedup-companies` auto-merges on exact domain and LLM-gates fuzzy pairs at high
confidence. An admin view surfacing medium-confidence pairs for manual approval
remains a possible enhancement.

### Missing-data residue after the 2026-07-19 wrong-website healing (#242)
#242 made news-article-as-website rows self-healing (14 healed on first
apply; watch `aggregator_url_reset` each cron ~0 steady-state). What's left
description-less, in order:
- **Healed-website re-resolution tail [S]**: the 14 reset slugs (incl.
  bespoke-labs, clio, alsym-energy, hydra-host) re-resolve + re-enrich over
  the next crons — spot-check a few after ISR; any that re-heal to a NEWS
  host again indicate a resolver gap, not a repair gap.
- **Cloudflare-403 scrape cohort [decision parked]**: sites reachable in a
  browser but 403ing Actions IPs (blue-origin's blueorigin.com may be one —
  check its scrape outcome next cron). "Route around, don't evade" stands.
- **Website-less residue [blocked on data]**: the re-mining pool is
  EXHAUSTED (2026-07-19 backfill dispatch: seen=0) — no Wikidata P856, no
  minable article link. Re-measure as scrape/discovery coverage grows.
- ~~**Structured-describe fallback ("A")**~~ — **SHIPPED end-to-end
  (#243 probe / #244 apply+0045 / #245 live goldens / #246 web gating /
  #247 supersede path) and BACKFILLED 2026-07-19**: 246 descriptions
  persisted across 4 batches (1,079-cohort drained; 0 errors; ~$0.35
  total). Remaining description-less ≈ 830 rows are the evidence-less
  residue (no Wikidata entity, no corroborated coverage) — honest empties;
  re-measure as coverage grows.
- **Non-US suspects from the fallback descriptions [S, ops queue]**: the
  backfill's dumb-regex flag surfaced shown companies whose own grounded
  descriptions read non-US — verify each, then ops exclude-company
  (reason non_us): zepto, clio, personio, oyo, groww, pine-labs,
  craftsvilla, net-a-porter, netlog, onefinestay, altair-semiconductor,
  axonius, bitstrips, bold-security, buddybuild, crew, jive, linear(?),
  manifest-law, outright, samples(?) — the (?) ones look like
  false-positive regex hits; eyeball before excluding anything.

### Prominence-override dry-run rejects (2026-07-20) — route to their real fixes
The unexclude-prominent dry-run surfaced 4 not_a_startup rows shielded by
questionable data; each needs its OWN flow, never a blanket unexclude:
- **blue** — the audit headliner re-confirmed: Blue Origin's $10B round
  misattributed to a music-band row. delete-round (slug blue, amount
  10000000000) + the retroactive audit item covers it.
- **mistral** — "founded 1976" in the judge detail = wrong-entity
  contamination (Mistral AI is 2023/French); inspect + likely wrong
  website/entity purge, then non_us if FR HQ confirms.
- **iceye** (FI) / **helsing** (DE) — non-US; correct end-state is
  excluded. Set hq_country via ops/inspect evidence so the reason reads
  non_us rather than not_a_startup (cosmetic; low priority).

### Deliberately deferred — with reasons
- **Accounts/auth** — localStorage watchlists cover the consumer need; auth adds
  email infra, privacy surface, and session bugs for zero differentiation today.
- **Public API** — free-tier egress (5GB/mo) + scraper abuse risk; quarterly
  static JSON/CSV dumps get most of the goodwill at none of the risk.
- **LLM-written narrative reports** ("State of AI Infra") — one hallucinated
  claim damages the trust that is our moat; aggregate-driven pages (themes,
  trends) say the same thing with sourceable numbers.
- **Email digest** — first true cost item (sending infra); RSS + page first.
