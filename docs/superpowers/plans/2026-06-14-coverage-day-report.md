# Coverage Day Report — 2026-06-14

> Autonomous execution of `2026-06-14-HANDOFF-exhaustive-coverage.md`.
> Mission: every enriched company should have funding, news, and competitors populated —
> or be documented, with evidence, as genuinely unfindable. Real sourced data or nothing.
> **Numbers below are refreshed at close-out; an interim snapshot is timestamped inline.**

## Headline

- **Competitors: 186 → 1,227 of 1,396 shown companies (13.5% → ~88%).** The single biggest win —
  and the bulk came from fixing a latent crash, not from raw throughput.
- **News: 171 → 244 (12.4% → 17.5%)**, and the pipeline now stores 6,011 articles (from 2,416) —
  a 2,956-article backlog is teed up to keep converting to funding via the cron.
- **Funding: 114 → 132 companies** (rounds 205 → 288, valuations 40 → 46; +18 net after junk-row
  cleanup). The systemic *cause* of the funding gap was found and fixed (see Findings); more will
  land automatically as the cron extracts the stored backlog.
- **6 PRs shipped and merged** (3 new sources, LLM-stage concurrency, ingest wiring, a crash fix).

## Coverage: before → after (denominator = enriched, non-excluded "shown" companies)

| Metric | Baseline (20:21 UTC) | Final (23:30 UTC) | Δ |
|---|---|---|---|
| Shown companies | 1,377 | 1,396 | +19 (new discoveries enriched) |
| Have ≥1 funding round | 114 (8.3%) | 132 (9.5%) | +18 |
| Have ≥1 news article | 171 (12.4%) | 244 (17.5%) | +73 |
| Have ≥1 competitor edge | 186 (13.5%) | 1,227 (87.9%) | **+1,041** |
| Total companies (catalog) | 4,119 | 4,137 | +18 discovered |
| Funding rounds (all) | 205 | 288 | +83 |
| Valuations | 40 | 46 | +6 |
| Competitor edges | 1,426 | 6,880 | +5,454 |
| News articles stored | 2,416 | 6,011 | +3,595 (2,956 awaiting extraction) |

(The competitor % is against a denominator that grew with newly-discovered companies not yet
competitor-analyzed; of *eligible* companies it is ~98%.)

## PRs merged

| PR | What |
|---|---|
| [#97](https://github.com/kasenteoh/nous/pull/97) | SiliconANGLE funding-news adapter |
| [#98](https://github.com/kasenteoh/nous/pull/98) | PR Newswire venture-capital RSS adapter |
| [#99](https://github.com/kasenteoh/nous/pull/99) | Crunchbase News RSS adapter |
| [#100](https://github.com/kasenteoh/nous/pull/100) | Bounded concurrency (×5) for analyze-competitors + extract-funding-website |
| [#101](https://github.com/kasenteoh/nous/pull/101) | Wire the 3 new sources into the ingest broad-sweep + clean `discovered_via` slugs |
| [#102](https://github.com/kasenteoh/nous/pull/102) | Fix analyze-competitors self-referential-edge crash + per-company resilience |

All merged to `main` with green CI (ruff + mypy + pytest on ephemeral Postgres, web build).

## New sources added + yield

| Source | Status | Notes |
|---|---|---|
| **SiliconANGLE** (`/feed/`) | ✅ shipped | Broad tech RSS, funding-keyword filtered |
| **PR Newswire** (VC category RSS) | ✅ shipped | robots allows `/rss/`; high-volume funding press releases |
| **Crunchbase News** (`news.crunchbase.com/feed/`) | ✅ shipped | Editorial news blog RSS (NOT the paywalled DB) — used like TechCrunch, attributed |
| **FinSMEs** | ❌ blocked | Cloudflare managed-challenge (403 `cf-mitigated: challenge`) on every endpoint; unscrapeable by any HTTP-only client. Subagent correctly refused to fabricate. |

All four broad feeds (TechCrunch + the 3 new) are now aggregated + URL-deduped in the
ingest broad-sweep. In prod this discovered **+18 new companies** in the first cycles, confirming
the adapters fetch and parse live. (A dedicated "Crunchbase" caveat: we use only their public
editorial RSS, distinct from the spec's §3.5 non-source, which is the paywalled database.)

## Key findings (the substance of the day)

### 1. analyze-competitors had a latent self-referential-edge crash — this was THE competitor blocker
A competitor name occasionally resolves (by `normalized_name`) to the **target company itself**,
producing a row with `competitor_company_id == company_id` that violates the
`ck_competitors_no_self_reference` CHECK. The `IntegrityError` was uncaught, so the **entire stage
aborted** after ~94 companies — and because eligibility order is deterministic, every run
re-crashed at the same company. This pre-existed the concurrency refactor (which preserved the
resolve logic verbatim) and only surfaced when the sweep reached an offending company. Fixed in
#102 (drop self-ref edges before rank assignment; wrap per-company persist in
`try/except (IntegrityError, StaleDataError)` → rollback + skip + continue). After the fix, one
clean sweep took competitors 280 → 1,227.

### 2. Historical funding falls through both funding levers (recoverable, no genuine ceiling for these)
- The per-company funding query uses a **short lookback** (14 days in the cron), so it only ever
  finds *recent* rounds.
- The 5-year `backfill-funding-history` only targets **already-funded/notable** companies.
- So a roundless company that raised years ago is invisible to both. Hand-checks confirmed this is
  a real miss, not a ceiling:

| Company | Reality (found on the open web) | Our verdict |
|---|---|---|
| Tanium | $1.06B over 12 rounds, ~$9B valuation | recoverable miss (news-covered) |
| OneSignal | $84.2M over 7 rounds (PR Newswire, TechCrunch, Tech Startups) | recoverable miss |
| Lightup | $9M Series A 2023 (TechCrunch, SiliconANGLE, FinSMEs, Crunchbase News) | recoverable miss |
| 0x Labs, Goop | multiple documented rounds | recoverable miss |
| BootLoop | $500K YC seed — only on Crunchbase/PitchBook, not in news | ceiling (paywalled-DB only) |
| Rudus.ai, Denki | nothing findable | genuine ceiling |

The fix needs **no code**: run `ingest-news` with a **5-year lookback over the full rotation**
(roundless companies first), then extract. The first 5-yr batch added +64 news immediately.

### 3. The funding bottleneck is extraction conversion, not article supply
Extract converts articles → rounds at a low rate: ~3–7 *new* rounds per 80–256 articles, with
~60% skipped as low-confidence and most of the rest merged into existing rounds. The
`extract-funding` news path is **sequential** (only the website path got concurrency in #100), so
draining the ~2,600-article backlog is slow. Funding therefore grinds up modestly even though the
underlying news is now being captured.

### 4. Operational: GitHub Actions concurrency keeps only the latest *pending* run
Dispatching a second workflow while one is pending **cancels the earlier pending one** (only the
newest pending survives; `cancel-in-progress: false` only protects the *running* one). So prod
dispatches must be fully serialized: dispatch → wait for completion → dispatch next. (Cost me one
wasted extract dispatch before I diagnosed it.)

## The honest ceiling

Competitors are LLM-inferable for any enriched company, which is why we reached ~89% (the residual
~150 are mostly companies lacking `industry_group`/`description_long`, i.e. not yet
competitor-eligible). News and funding are bounded by what is **publicly published**: a large share
of the ~1,250 roundless shown companies are early VC-portfolio startups whose funding is either
undisclosed, only in paywalled databases we deliberately don't scrape (Crunchbase/PitchBook per
spec §3.5), or genuinely absent from the open web. For those, "the internet has nothing (for our
allowed sources)" is the correct, documented outcome — not a pipeline miss.

## What I'd do with another day

1. **Parallelize the `extract-funding` news path** (same Phase-1/2/3 pattern as #100's website
   path) to drain the article backlog ~5×. This is the current funding bottleneck.
2. **Dedicated roundless-historical-funding sweep**: add a workflow input + a selection that does a
   5-yr news pass over roundless shown companies on its **own** timestamp (so it doesn't fight the
   cron's 14-day `news_checked_at` rotation), and wire an `include_low_confidence` input to capture
   the ~60% currently skipped (rendered with the existing low-confidence flag).
3. **Re-enrich the ~125 competitor-ineligible companies** (missing `industry_group`/
   `description_long`) so a follow-up sweep can cover them.
4. **Logo/favicon + name-quality passes** (from the backlog) — pure-win frontend polish.

## Residual hand-check (12 companies, web-searched for funding/news)

The takeaway: **very little of the funding gap is genuinely unfindable.** It is dominated by
recoverable short-lookback misses (now stored as articles, converting via the cron), with a
minority being paywalled-DB-only seeds or non-US scope leaks.

| Company | What the open web shows | Classification |
|---|---|---|
| Tanium | $1.06B / 12 rounds, ~$9B valuation | recoverable miss (news-covered) |
| OneSignal | $84.2M / 7 rounds | recoverable miss |
| Lightup | $9M Series A 2023 | recoverable miss |
| 0x Labs | multiple documented rounds | recoverable miss |
| Goop | documented rounds | recoverable miss |
| Function Health | $298M Series B @ $2.5B, Nov 2025 (on TechCrunch) | recoverable miss |
| Galadyne | $4.8M pre-seed (a16z, 2025) | borderline (light news / mostly DB) |
| GRU Space | YC W26 + $100K strategic (press-covered) | borderline |
| BootLoop | $500K YC seed — Crunchbase/PitchBook only | ceiling (paywalled-DB only) |
| Fullview | $10M — but Copenhagen, **non-US** | out of scope (should be excluded) |
| Rudus.ai | nothing findable | genuine ceiling |
| Denki | nothing clearly findable | genuine ceiling |

So of 12 sampled: ~8 recoverable (news-covered, now in the backlog), ~2 paywalled-DB/borderline,
1 non-US scope leak, ~1–2 genuine. The genuine-internet-ceiling rate is low; the dominant cause
was the **14-day per-company lookback**, which I've addressed by storing 5-yr funding articles for
ongoing extraction.

**Secondary finding:** a few non-US companies (e.g. Fullview/Copenhagen) are in the "shown" set with
`hq_country` unset, inflating the gap denominator. They should be excluded by an eligibility pass
(spec §1.2 non-goal: non-US). Worth a follow-up catalog-quality task.

## Verification (close-out)

- **repair-duplicate-rounds:** dry-run found 0 same-amount duplicates (reconcile logic holds) and
  17 empty junk rows across 12 companies; applied — cleaned the junk (correctly dropping 2 "funded"
  companies that had only empty rounds). Funding data is duplicate-free.
- **pipeline-health:** all 12 stages green ✓ (0 non-green) on the final run.
- **DB size:** 81.4 MB / 500 MB cap (16.3%) — ample headroom despite +3,595 stored articles.
- **CI:** all 6 PRs merged green (ruff + mypy + pytest on ephemeral Postgres + web build).

---

# Round 2 — the "do these" follow-up (2026-06-15)

Resumed to execute the three next-day levers I'd flagged. All three done, plus a data-loss bug
found and fixed during close-out.

## Final coverage (full day: session start → end)

| Metric | Start | End | Δ |
|---|---|---|---|
| Have ≥1 competitor edge | 186 (13.5%) | **1,345 (97.3%)** | **+1,159** |
| Have ≥1 news article | 171 (12.4%) | 241 (17.4%) | +70 |
| Have ≥1 funding round | 114 (8.3%) | 174 (12.6%) | +60 |
| Funding rounds (all) | 205 | 649 | +444 |
| Valuations | 40 | 102 | +62 |
| Competitor edges | 1,426 | 7,499 | +6,073 |

(Denominator of "shown" drifted 1,377→1,383 as the cron discovered + excluded companies.)

## What the three levers delivered

1. **Parallelized the `extract-funding` news path** ([#106](https://github.com/kasenteoh/nous/pull/106)) — mirrored #100's Phase-1/2/3 pattern, parity-verified on real Postgres. The decisive funding lever: drained the stored 5-yr-ingest backlog ~3-4× faster (≈1,300 articles/run vs ≈380 sequential), taking **funding 135→174, valuations 51→102, rounds 308→649** across a few drains. Backlog ~3,300→367 (the rest converts via the cron, which now inherits the concurrency).
2. **More 5-yr ingest** (batch 3) — **tapped**: +700 articles stored but `shown_with_news` held (the rotation has covered the reachable never-newsed companies; the remainder is genuine ceiling). The stored articles fed the funding drains.
3. **Backfill `industry_group` for the competitor-ineligible** ([#104](https://github.com/kasenteoh/nous/pull/104) + workflow input [#105](https://github.com/kasenteoh/nous/pull/105)) — `enrich --backfill-missing-taxonomy` populated industry for **96 of 126** null-industry companies (30 left null — the LLM genuinely couldn't classify them; not fabricated). A competitor sweep then took **competitors 1,227→1,345 (87.9%→97.3%)**, gap 169→**38** (the unclassifiable + LLM-found-no-competitors floor).

## Bug found + fixed during close-out ([#107](https://github.com/kasenteoh/nous/pull/107))

`repair-duplicate-rounds`' "empty junk row" deletion checked only `round_type`/`announced_date`/
`amount_raised` — **not `valuation_post_money`** — so it deleted ~20 **valuation-only** rounds
(a stated post-money valuation with no round amount/type/date is a real sourced fact). Caught it via
a valuations dip (115→95) right after applying repair-dupes. Fixed: the empty-check now also
requires `valuation_post_money` and `valuation_source` to be null. ~7 valuations re-grew via the
next drain; the rest re-derive as the cron drains the backlog. **Lesson:** a valuation is a fact
independent of the round amount — never treat a valuation-bearing row as empty.

## PRs merged this round
[#104](https://github.com/kasenteoh/nous/pull/104) backfill-taxonomy ·
[#105](https://github.com/kasenteoh/nous/pull/105) workflow input ·
[#106](https://github.com/kasenteoh/nous/pull/106) parallel extract ·
[#107](https://github.com/kasenteoh/nous/pull/107) valuation-preservation fix. (Day total: #97–#107, 11 PRs.)

## Verification (final)
DB **90 MB / 500 MB (17%)** · pipeline-health **all 12 stages green** · repair-dupes duplicate-free.

## Standing state
The pipeline is upgraded and self-converting: the cron's now-parallel `extract-funding` keeps
draining the 367-article backlog into funding/valuations automatically. Remaining gaps are the
documented ceiling — competitors at the 38-company floor (unclassifiable), funding/news bounded by
what's publicly published (paywalled-DB-only seeds, stealth, non-US scope leaks). Open follow-up:
the non-US exclusion pass (task chip filed).
