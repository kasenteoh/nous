# Funding / news separation on the company page — design

**Date:** 2026-07-18 · **Status:** approved by owner (brainstorm session,
layout option A selected via visual companion; coverage-home question
answered "stays with its round")

## Problem

`/c/[slug]` renders one merged **Timeline** section (`EventTimeline`,
#170/#194/#239) interleaving funding rounds and news stories on a single
rail. The owner wants them separated: the timeline view they like should be
funding only, with news in its own place. The merged view buries the funding
structure — the page's spine — among coverage rows.

## Decision (owner-approved)

**Layout A — two stacked sections.** The single-column page rhythm is kept;
no tabs (hides content from scanning and crawlers), no two-column split
(breaks the page's rhythm, cramps mobile).

1. **"Funding"** — the rail timeline, rounds only.
2. **"In the news"** — a compact list directly beneath, standalone stories
   only.

**Coverage home:** an article that covers a specific round STAYS collapsed
under that round in the Funding section ("Covered by TechCrunch, Reuters +2
more") — coverage is evidence for the round. The news section shows ONLY
standalone stories (the #239 clusters that match no round: rumors, IPO
chatter, pre-announcement coverage). Every article appears exactly once.

## Components

Split `EventTimeline` into two server components (pure presentation; the
pure lib `web/lib/timeline.ts` `buildTimeline` already computes both halves
and is UNCHANGED):

- **`FundingTimeline`** (`web/components/FundingTimeline.tsx`): the
  `kind === "funding"` items. Keeps everything a round row has today —
  rail visual (money-green markers), amount/valuation/investors, the
  ✓ `VerifiedBadge`, extraction-confidence tooltip + low-confidence pill,
  single-source inline `SourceLink`, and the collapsed `CoverageDisclosure`
  for ≥2 sources. Section header **"Funding"**. Ordering unchanged
  (undated funding leads, dated newest-first).
- **`NewsSection`** (`web/components/NewsSection.tsx`): the
  `kind === "news"` items (story clusters). Each story: lead headline
  (external link) + date + source host, `CoverageDisclosure` when the
  story has ≥2 syndicated sources. Visually muted/compact relative to the
  funding rail (list rows, not a second rail). Section header
  **"In the news"**.
- `EventTimeline.tsx` is deleted; `page.tsx` calls `buildTimeline` ONCE,
  splits items by kind, and renders `<FundingTimeline items={…}/>` then
  `<NewsSection items={…}/>` in the same page position. Components take
  pre-split items (no double computation); the page owns the both-empty
  line. `CoverageDisclosure` moves to a small shared module — one
  implementation, both consumers.

## Behavior & edge cases

- **Empty news** → "In the news" omits entirely (site convention).
- **Rounds empty, news present** → "Funding" omits; news renders alone.
- **Both empty** → single muted "No funding rounds or news recorded yet."
  (today's empty state, rendered by the page since it owns the split).
- **Long news lists**: newest 8 stories visible; the rest inside a native
  `<details>` "Show N older stories" (server-component-safe, keyboard
  operable). Nothing is ever dropped — trust invariant.
- **Verified badges**: only funding facts carry ✓ today; NewsSection has no
  badge surface. `verified` map stays a FundingTimeline prop.
- The `.md` sibling already separates "Funding rounds" / "Recent coverage"
  — no change.

## Testing

- Component tests split: existing `event-timeline-coverage.test.tsx`
  becomes tests for FundingTimeline (coverage disclosure, badges,
  confidence) + new NewsSection tests (story rows, disclosure, the 8-story
  cap + details, omit-when-empty).
- `web/test/timeline.test.ts` (pure lib) is untouched — `buildTimeline`'s
  contract is unchanged.
- Page-level: verify both sections render in order and the empty states
  (existing page tests / e2e structural block).

## Out of scope

- Any pipeline/schema/query change (read-time presentational split only).
- Re-styling the round rows themselves.
- News on other surfaces (/new, feeds, .md) — unchanged.
