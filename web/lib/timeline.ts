// Pure timeline assembly for /c/[slug]: cluster a company's funding-announcement
// news UNDER the funding round it covers, so one well-covered raise renders as a
// single round entry with its press collapsed — not N near-duplicate news rows.
//
// nous only ingests funding-announcement news (ingest-news filters on
// is_funding_announcement), so the "news" IS the funding coverage: one round is
// typically covered by many outlets, each a separate news_articles row.
// Attachment precedence per article: (a0) the PERSISTED link — the pipeline
// records which round each article's extraction reconciled into
// (news_articles.funding_round_id, migration 0044) — is exact and wins
// outright; (a) a round's primary_news_url pins its own announcement; (b)
// legacy/unlinked articles cluster by date proximity to the round's
// announced_date. Trust-preserving: every article stays one click away
// (collapsed, never dropped) — the moat is intact, and multi-outlet coverage
// becomes a positive "widely covered" signal.
//
// Pure + side-effect-free (no DB, no React) so it is unit-testable and the
// EventTimeline component stays pure presentation.

import type { FundingRoundWithInvestors, NewsArticleRow } from "@/lib/types";
import { httpHost } from "@/lib/url";

/** Max |published_date − announced_date| (days) for a news article to attach to
 *  a round. Funding press clusters within days; the slack covers late write-ups. */
export const MATCH_WINDOW_DAYS = 14;

const DAY_MS = 86_400_000;

/** One source link under a round: the article's title (null for a round's
 *  `primary_news_url` that has no matching news_articles row — rendered as just
 *  its host) and the display host. */
export interface CoverageLink {
  url: string;
  title: string | null;
  host: string;
}

export type TimelineItem =
  | {
      kind: "funding";
      round: FundingRoundWithInvestors;
      /** Deduped source coverage, primary source first then newest-first. */
      coverage: CoverageLink[];
    }
  | { kind: "news"; article: NewsArticleRow };

/** Canonical dedup key (host + path, www/scheme/query/trailing-slash-insensitive)
 *  so the same story via http/https/www/tracking-params collapses to one row, and
 *  a round's own article isn't double-counted. Null for a non-http(s) URL. */
function canonicalUrl(url: string): string | null {
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    const host = u.hostname.toLowerCase().replace(/^www\./, "");
    const path = u.pathname.replace(/\/+$/, "");
    return `${host}${path}`;
  } catch {
    return null;
  }
}

/** Whole-day gap between two ISO date strings (both stored as YYYY-MM-DD → UTC
 *  midnight, so no DST drift). */
function dayGap(a: string, b: string): number {
  return Math.round(Math.abs(Date.parse(a) - Date.parse(b)) / DAY_MS);
}

/** Assemble one round's coverage from its clustered news articles + its
 *  `primary_news_url`: dedup by canonical URL, drop unparseable, put the primary
 *  source FIRST, and keep the rest newest-first. */
function assembleCoverage(
  round: FundingRoundWithInvestors,
  clustered: NewsArticleRow[],
): CoverageLink[] {
  // Newest-first among the clustered articles (nulls — impossible here, they were
  // filtered — sort last defensively).
  const ordered = [...clustered].sort((a, b) =>
    (b.published_date ?? "").localeCompare(a.published_date ?? ""),
  );

  const seen = new Set<string>();
  const links: CoverageLink[] = [];
  for (const article of ordered) {
    const key = canonicalUrl(article.url);
    const host = httpHost(article.url);
    if (key === null || host === null || seen.has(key)) continue;
    seen.add(key);
    links.push({ url: article.url, title: article.title, host });
  }

  // Fold in the round's primary source: move it to the front if already present,
  // else prepend a title-less entry (we have no news_articles row for it, so only
  // its host renders).
  const primary = round.primary_news_url;
  if (primary) {
    const primaryKey = canonicalUrl(primary);
    const primaryHost = httpHost(primary);
    if (primaryKey !== null && primaryHost !== null) {
      const idx = links.findIndex((l) => canonicalUrl(l.url) === primaryKey);
      if (idx > 0) {
        const [existing] = links.splice(idx, 1);
        links.unshift(existing);
      } else if (idx === -1) {
        links.unshift({ url: primary, title: null, host: primaryHost });
      }
    }
  }

  return links;
}

/** Ordering tier (mirrors the prior EventTimeline logic): undated funding leads
 *  (0), dated events run newest-first (1), undated news trails (2). */
function itemDate(item: TimelineItem): string | null {
  return item.kind === "funding"
    ? item.round.announced_date
    : item.article.published_date;
}

function tier(item: TimelineItem): number {
  if (itemDate(item)) return 1;
  return item.kind === "funding" ? 0 : 2;
}

/**
 * Assemble the ordered timeline: funding rounds (each with its clustered,
 * deduped coverage) plus standalone news (articles that match no round).
 *
 * Clustering: an article whose `funding_round_id` names one of this company's
 * rounds attaches there — the pipeline-recorded exact link, independent of
 * dates (this is what groups coverage under UNDATED rounds). Otherwise the
 * article attaches to the funding round whose `announced_date` is NEAREST to
 * the article's `published_date`, within `MATCH_WINDOW_DAYS`. Ties → the
 * larger `amount_raised`. An article with no `published_date`, or no round
 * in-window (incl. when no round has a date), becomes a standalone `news`
 * item — nothing is dropped. A `funding_round_id` that matches none of the
 * passed rounds (an orphaned link after a round delete) falls back to the
 * date path rather than vanishing.
 *
 * Pure and deterministic given (rounds, news).
 */
export function buildTimeline(
  rounds: FundingRoundWithInvestors[],
  news: NewsArticleRow[],
): TimelineItem[] {
  // Exclude articles whose URL can't render a real link (unparseable / non-http(s)):
  // a dead link is not a source, so it is dropped CONSISTENTLY — never as coverage
  // and never as a dead standalone row (news_articles.url is http(s) in practice,
  // so this is defensive symmetry, not a common path).
  const renderable = news.filter((article) => httpHost(article.url) !== null);

  const datedRounds = rounds.filter((r) => r.announced_date !== null);

  // Pin each round's OWN primary_news_url article to that round BEFORE date
  // clustering, keyed by canonical URL. Two bugs this prevents: a round's
  // announcement being pulled onto a NEIGHBORING round by nearest-wins (a bridge
  // + a Series A within 14 days would misattribute each other's press), and the
  // primary double-rendering (as the round's source AND a standalone/other-round
  // row) when its news_articles row is null-dated or out-of-window.
  const primaryOwner = new Map<string, string>(); // canonical URL → round id
  for (const round of rounds) {
    if (round.primary_news_url === null) continue;
    const key = canonicalUrl(round.primary_news_url);
    if (key !== null && !primaryOwner.has(key)) primaryOwner.set(key, round.id);
  }

  const coverageByRound = new Map<string, NewsArticleRow[]>();
  const standalone: NewsArticleRow[] = [];
  const attach = (roundId: string, article: NewsArticleRow): void => {
    const arr = coverageByRound.get(roundId) ?? [];
    arr.push(article);
    coverageByRound.set(roundId, arr);
  };

  const roundIds = new Set(rounds.map((r) => r.id));

  for (const article of renderable) {
    // (a0) The pipeline recorded EXACTLY which round this article's extraction
    //      reconciled into (0044) → attach there, no guessing. Orphaned links
    //      (round since deleted/merged) fall through to the heuristics.
    if (
      article.funding_round_id !== null &&
      roundIds.has(article.funding_round_id)
    ) {
      attach(article.funding_round_id, article);
      continue;
    }
    // (a) The article IS a round's primary source → it belongs to that round,
    //     regardless of its own date (pinned above), never a neighbor.
    const canon = canonicalUrl(article.url);
    const owner = canon === null ? undefined : primaryOwner.get(canon);
    if (owner !== undefined) {
      attach(owner, article);
      continue;
    }
    // (b) Otherwise cluster to the NEAREST dated round within the window.
    const published = article.published_date;
    if (published === null) {
      standalone.push(article);
      continue;
    }
    let best: FundingRoundWithInvestors | null = null;
    let bestGap = Number.POSITIVE_INFINITY;
    for (const round of datedRounds) {
      const gap = dayGap(published, round.announced_date as string);
      if (gap > MATCH_WINDOW_DAYS) continue;
      const closer = gap < bestGap;
      const tieToLargerRound =
        gap === bestGap &&
        (round.amount_raised ?? 0) > (best?.amount_raised ?? 0);
      if (closer || tieToLargerRound) {
        best = round;
        bestGap = gap;
      }
    }
    if (best) {
      attach(best.id, article);
    } else {
      standalone.push(article);
    }
  }

  const items: TimelineItem[] = [
    ...rounds.map(
      (round): TimelineItem => ({
        kind: "funding",
        round,
        coverage: assembleCoverage(round, coverageByRound.get(round.id) ?? []),
      }),
    ),
    ...standalone.map((article): TimelineItem => ({ kind: "news", article })),
  ];

  return items.sort((a, b) => {
    const ta = tier(a);
    const tb = tier(b);
    if (ta !== tb) return ta - tb;
    return (itemDate(b) ?? "").localeCompare(itemDate(a) ?? "");
  });
}
