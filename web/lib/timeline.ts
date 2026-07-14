// Pure timeline assembly for /c/[slug]: cluster a company's funding-announcement
// news UNDER the funding round it covers, so one well-covered raise renders as a
// single round entry with its press collapsed — not N near-duplicate news rows.
//
// nous only ingests funding-announcement news (ingest-news filters on
// is_funding_announcement), so the "news" IS the funding coverage: one round is
// typically covered by many outlets, each a separate news_articles row. There is
// no news→round link in the data, so we cluster read-time by date proximity to
// the round's announced_date. Trust-preserving: every article stays one click
// away (collapsed, never dropped) — the moat is intact, and multi-outlet coverage
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
 * Clustering: each news article attaches to the funding round whose
 * `announced_date` is NEAREST to the article's `published_date`, within
 * `MATCH_WINDOW_DAYS`. Ties → the larger `amount_raised`. An article with no
 * `published_date`, or no round in-window (incl. when no round has a date),
 * becomes a standalone `news` item — nothing is dropped.
 *
 * Pure and deterministic given (rounds, news).
 */
export function buildTimeline(
  rounds: FundingRoundWithInvestors[],
  news: NewsArticleRow[],
): TimelineItem[] {
  const datedRounds = rounds.filter((r) => r.announced_date !== null);
  const coverageByRound = new Map<string, NewsArticleRow[]>();
  const standalone: NewsArticleRow[] = [];

  for (const article of news) {
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
      const arr = coverageByRound.get(best.id) ?? [];
      arr.push(article);
      coverageByRound.set(best.id, arr);
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
