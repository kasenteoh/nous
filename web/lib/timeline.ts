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
// consuming components (FundingTimeline / NewsSection — the page splits one
// buildTimeline result by kind) stay pure presentation.

import type { FundingRoundWithInvestors, NewsArticleRow } from "@/lib/types";
import { httpHost } from "@/lib/url";

/** Max |published_date − announced_date| (days) for a news article to attach to
 *  a round. Funding press clusters within days; the slack covers late write-ups. */
export const MATCH_WINDOW_DAYS = 14;

/** Max |published_date| gap (days) for two STANDALONE articles to be the same
 *  story. Syndications of one piece land within days of each other; a week of
 *  slack covers slow re-prints without gluing separate events together (the
 *  title-similarity bar below is the real discriminator). */
export const STORY_WINDOW_DAYS = 7;

/** Minimum normalized-title overlap (|A∩B| / min(|A|,|B|)) for two standalone
 *  articles to merge into one story, plus an absolute shared-token floor so
 *  two short titles can't merge on two common words. Calibrated on the
 *  observed firehose shapes: identical syndicated headlines score 1.0;
 *  "seeks $10B" rumor coverage vs "$2B committed" coverage scores ~0.5. */
export const STORY_SIMILARITY_MIN = 0.6;
export const STORY_SHARED_TOKENS_MIN = 3;

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
  | {
      kind: "news";
      /** The story's lead article (newest in its cluster) — its title/date
       *  render the row. */
      article: NewsArticleRow;
      /** Every article in the story cluster (lead first, then newest-first).
       *  Length 1 = a genuinely standalone article; ≥2 = one story covered by
       *  several outlets, rendered collapsed like round coverage. */
      coverage: CoverageLink[];
    };

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

// ---------------------------------------------------------------------------
// Standalone-story clustering. Articles that attach to no round (undated
// rounds, out-of-window press, rumor-era coverage) used to render as one
// timeline row EACH — the same syndicated story ×10-35 rows (the kalshi /
// blue-origin firehose, 2026-07-17 QA). Cluster them by normalized-title
// similarity within a date window and render each story once, with the same
// collapsed "Covered by" treatment round coverage gets. Trust-preserving:
// every article stays one click away, never dropped.
// ---------------------------------------------------------------------------

/** Words too common in funding headlines to signal story identity. */
const TITLE_STOPWORDS = new Set([
  "a",
  "an",
  "and",
  "as",
  "at",
  "by",
  "for",
  "from",
  "in",
  "into",
  "its",
  "of",
  "on",
  "the",
  "to",
  "with",
  "after",
  "amid",
  "report",
  "reportedly",
  "exclusive",
  "news",
]);

/** Fold announce-verb variants so "raises"/"raised"/"raising" don't split a
 *  story; deliberately tiny — the amount + entity tokens do the real work. */
const VERB_FOLDS = new Map([
  ["raises", "raise"],
  ["raised", "raise"],
  ["raising", "raise"],
  ["secures", "raise"],
  ["secured", "raise"],
  ["closes", "raise"],
  ["closed", "raise"],
  ["lands", "raise"],
  ["nabs", "raise"],
  ["bags", "raise"],
  ["gets", "raise"],
]);

/**
 * Normalized token set for story-identity comparison: the trailing
 * " - Outlet" / " | Outlet" segment is stripped (the same headline syndicated
 * by four outlets differs ONLY there), money spellings fold together
 * ("$10bn" / "$10 billion" / "10B" → "10 billion"), announce-verbs fold, and
 * stopwords drop. Possessives lose their "'s" so "Origin's" matches "Origin".
 */
export interface TitleSignature {
  tokens: Set<string>;
  /** Normalized money mentions ("2 billion", "300 million") — the strongest
   *  story discriminator: "seeks $10B" and "put $2B in" share plenty of
   *  entity words but are different events. */
  money: Set<string>;
}

export function titleTokens(title: string): TitleSignature {
  // Strip the trailing "- Outlet" segment — but not a dash-CLAUSE that is
  // real title content ("… - and it's just the start"): a segment starting
  // with a clause word survives (review catch).
  const withoutOutlet = title.replace(
    /\s+[-–—|]\s+(?!(?:and|but|as|a|an|the|it|its|here|what|why|how|not)\b)[^-–—|]{1,40}$/i,
    "",
  );
  const folded = withoutOutlet
    .toLowerCase()
    .replace(/[’']s\b/g, "")
    // "$10bn" / "$10b" → "10 billion"; bare short suffixes ("10b", "5m
    // users") need the $ anchor or they false-fold (review catch). The
    // spelled-out words fold bare: "10 billion".
    .replace(/\$(\d+(?:\.\d+)?)\s*(?:bn|b|billion)\b/g, "$1 billion")
    .replace(/\$(\d+(?:\.\d+)?)\s*(?:mn|m|million)\b/g, "$1 million")
    .replace(/(\d+(?:\.\d+)?)\s+(billion|million)\b/g, "$1 $2")
    .replace(/\$(\d)/g, "$1");
  const tokens = new Set<string>();
  for (const raw of folded.split(/[^a-z0-9.]+/)) {
    if (!raw) continue;
    const word = VERB_FOLDS.get(raw) ?? raw;
    if (TITLE_STOPWORDS.has(word)) continue;
    tokens.add(word);
  }
  const money = new Set<string>();
  for (const m of folded.matchAll(/(\d+(?:\.\d+)?) (billion|million)/g)) {
    money.add(`${m[1]} ${m[2]}`);
  }
  return { tokens, money };
}

/** Overlap coefficient with an absolute shared-token floor, vetoed by
 *  disjoint money mentions (when both titles name amounts). */
function sameStory(a: TitleSignature, b: TitleSignature): boolean {
  if (a.tokens.size === 0 || b.tokens.size === 0) return false;
  if (a.money.size > 0 && b.money.size > 0) {
    let moneyShared = false;
    for (const m of a.money) if (b.money.has(m)) moneyShared = true;
    if (!moneyShared) return false;
  }
  let shared = 0;
  for (const t of a.tokens) if (b.tokens.has(t)) shared += 1;
  if (shared < STORY_SHARED_TOKENS_MIN) return false;
  return shared / Math.min(a.tokens.size, b.tokens.size) >= STORY_SIMILARITY_MIN;
}

/**
 * Greedy newest-first clustering of standalone articles into stories. Each
 * article joins the first existing cluster whose LEAD it matches (same story
 * by title, within STORY_WINDOW_DAYS of the lead's date); otherwise it opens
 * a new cluster. Undated articles never merge (no window to reason about —
 * one row each, exactly the old behavior). Deterministic given input order.
 */
function clusterStories(standalone: NewsArticleRow[]): NewsArticleRow[][] {
  const ordered = [...standalone].sort((a, b) =>
    (b.published_date ?? "").localeCompare(a.published_date ?? ""),
  );
  const clusters: {
    lead: NewsArticleRow;
    leadTokens: TitleSignature;
    all: NewsArticleRow[];
  }[] = [];
  for (const article of ordered) {
    const tokens = titleTokens(article.title);
    const match =
      article.published_date === null
        ? undefined
        : clusters.find(
            (c) =>
              c.lead.published_date !== null &&
              dayGap(
                article.published_date as string,
                c.lead.published_date as string,
              ) <= STORY_WINDOW_DAYS &&
              sameStory(tokens, c.leadTokens),
          );
    if (match) {
      match.all.push(article);
    } else {
      clusters.push({ lead: article, leadTokens: tokens, all: [article] });
    }
  }
  return clusters.map((c) => c.all);
}

/** A story cluster's coverage links: lead first (it renders the row title),
 *  then the rest newest-first; deduped by canonical URL. */
function storyCoverage(cluster: NewsArticleRow[]): CoverageLink[] {
  const seen = new Set<string>();
  const links: CoverageLink[] = [];
  for (const article of cluster) {
    const key = canonicalUrl(article.url);
    const host = httpHost(article.url);
    if (key === null || host === null || seen.has(key)) continue;
    seen.add(key);
    links.push({ url: article.url, title: article.title, host });
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
    ...clusterStories(standalone).map(
      (cluster): TimelineItem => ({
        kind: "news",
        article: cluster[0],
        coverage: storyCoverage(cluster),
      }),
    ),
  ];

  return items.sort((a, b) => {
    const ta = tier(a);
    const tb = tier(b);
    if (ta !== tb) return ta - tb;
    return (itemDate(b) ?? "").localeCompare(itemDate(a) ?? "");
  });
}
