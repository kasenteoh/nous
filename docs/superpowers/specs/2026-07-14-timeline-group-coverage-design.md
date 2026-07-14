# Design — Timeline: group funding coverage under its round

Written 2026-07-14, brainstormed + owner-approved. Reduces `/c/[slug]` timeline
clutter by nesting a funding round's press coverage under the round instead of
rendering every article as its own entry.

## Problem

The `Timeline` (`web/components/EventTimeline.tsx`) merges funding rounds + news
into one reverse-chronological list. But `ingest-news` only ingests **funding
announcements** (filtered on `is_funding_announcement`, from six funding-news
feeds), so the "news" *is* the funding coverage. One round (e.g. Blue Origin's
raise) therefore renders as: the structured round entry, **plus** its
`primary_news_url` article again as a standalone news entry (no dedup), **plus**
every other outlet's article about the same raise — each a separate
`news_articles` row → a separate timeline entry. There is no `news_article →
funding_round` link in the data, news is uncapped, and nothing clusters
"same-event" articles. Result: heavy clutter on well-covered companies.

## Approach (owner-approved): group coverage under the round — read-time first

Cluster each news article to the funding round it covers and render the round as
the primary item with its coverage collapsed. **Trust-preserving:** every article
stays one click away (collapsed, never dropped) — the moat ("every fact sourced")
is intact, and multi-outlet coverage becomes a positive "widely covered" signal.
Read-time only: no migration, no pipeline change, fully reversible.

## Logic — `web/lib/timeline.ts` (pure, unit-tested)

`buildTimeline(rounds, news): TimelineItem[]` where
`TimelineItem = { kind: "funding"; round; coverage: CoverageLink[] }
              | { kind: "news"; article }`.

**Clustering (news → round):**
- A news article attaches to the funding round whose `announced_date` is
  **nearest to the article's `published_date`, within `MATCH_WINDOW_DAYS` (14)**.
  Ties (equal gap) → the larger `amount_raised`. Rounds with a null
  `announced_date` are not match candidates (can't date-cluster).
- A news article with a null `published_date`, or no round within the window, or
  when the company has no dated rounds → becomes a **standalone** `news` item
  (nothing is dropped).

**Coverage assembly per round:**
1. `clustered` = news articles assigned to this round, **deduped by canonical
   URL** (first wins).
2. Each coverage entry is a `CoverageLink { url; title: string | null; host }`.
   The article's `title`/`source` populate it.
3. If `round.primary_news_url` is set and its canonical URL is **not** already in
   `clustered`, prepend a title-less coverage entry for it (rendered as just its
   host). If it **is** in `clustered`, move that entry to the front. So the
   **primary source always leads**; the rest sort by `published_date` desc.
4. Unparseable URLs are dropped from coverage (mirrors `Sources`/`SourceLink`).

**Ordering** of `TimelineItem`s keeps the existing tier logic
(`EventTimeline.timelineTier`): undated funding leads, dated events run
newest-first, undated news trails.

## Rendering — `EventTimeline.tsx`

- **`coverage.length >= 2`** → a collapsed native `<details>` (zero client JS,
  server-component-safe): `<summary>` reads "Covered by {host0}, {host1}
  +{N-2} sources" (or "Covered by {host0}, {host1}" when N==2). Expanded lists
  each coverage entry as a `title · host` external link (or just the `host` link
  when title-less). The per-round inline `↗` `SourceLink` is **removed** for these
  rounds — the coverage list subsumes it (and includes the primary).
- **`coverage.length <= 1`** → unchanged: the existing single `↗` `SourceLink`
  to the round's `primary_news_url` (no expander — no clutter on lightly-covered
  rounds).
- **Standalone `news` items** → unchanged (`NewsEntry`).

Muted vocabulary (`text-ink-muted`/`text-ink-faint` per the a11y-fixed tokens),
`<details>`/`<summary>` styled to match the timeline; the chevron rotates on open.

## Defaults (tunable)

- `MATCH_WINDOW_DAYS = 14` (funding press clusters within days; slack for late
  write-ups).
- Expanded coverage shows **all** sources (no cap — provenance over brevity).
- Collapse threshold: `>= 2` coverage entries.

## Tests

- `buildTimeline`: nearest-in-window assignment; the ±14d boundary (in vs out);
  tie → larger round; null `published_date` / null `announced_date` / no-rounds →
  standalone; `primary_news_url` dedup + lead ordering; coverage URL dedup;
  unparseable URL dropped; the double-render (round's own article) collapses.
- `EventTimeline` render: ≥2 coverage → collapsed summary + expandable list, no
  standalone `↗`; ==1 coverage → the single `↗`, no expander; standalone news
  still renders; every coverage link is present + external.

## Scope / follow-up

One read-time web PR (`fable5/timeline-group-coverage`): the pure helper +
`EventTimeline` + tests. No migration, no pipeline change. If the date-proximity
mapping proves accurate against the real build, a **follow-up** may persist a
`news_articles.funding_round_id` link (a pipeline classification step) for exact
grouping — but read-time validates the UX first.

## Verification

`npm run lint` + `npm run test` + `npm run build` in `web/`. Adversarial
`code-reviewer` over the branch diff before merge.
