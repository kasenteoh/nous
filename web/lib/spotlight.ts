// Spotlight pool — the front page "activity blend" (spec §4).
//
// Computed at render time from three cheap queries plus in-process scoring.
// Current data volume is hundreds–low-thousands of rows; this is deliberate
// YAGNI — a SQL view is the future optimization, not today's. Scoring and
// shuffling are pure functions so they stay testable.
//
// Product rule: spotlighted companies MUST have at least one funding round on
// record. Funding is the credibility bar for the front page — news-only or
// recently-added companies without documented rounds must not appear in
// "Today's spotlight". This is enforced by fetching the full set of funded
// company IDs early and filtering every candidate and fill path against it.

import { createSupabaseServerClient } from "@/lib/db";
import { formatEmployeeRange, formatLocation } from "@/lib/format";

// ─── Scoring weights (spec §4 — keep every tunable here) ─────────────────────

const FUNDING_WINDOW_DAYS = 120;
const NEWS_WINDOW_DAYS = 30;
const FRESHNESS_WINDOW_DAYS = 30;

/** Funding recency: 3 × (1 − days_since/120), so a round today is worth 3. */
const FUNDING_RECENCY_WEIGHT = 3;
/** Amount bonus: log10(amount)/3 — log10($50M)≈7.7 → +2.6. */
const AMOUNT_LOG_DIVISOR = 3;
/** News: 0.5 per article in the last 30d, capped. */
const NEWS_WEIGHT = 0.5;
const NEWS_COUNT_CAP = 10;
/** Companies created within the last 30d. */
const FRESHNESS_BONUS = 1.5;

const POOL_SIZE = 10;
// Eligibility (description_short present, status=active, ≥1 funding round) is
// applied by the display fetch and the funded-ids gate, so rank more candidates
// than the pool needs to absorb ineligible ones.
const CANDIDATE_LIMIT = 30;

// Page size for the keyset scan that collects all funded company IDs.
// PostgREST hard-caps every response at 1000 rows regardless of .limit(), so
// raising FUNDED_IDS_PAGE_SIZE would do nothing — pagination is the only way to
// read more than 1000 rows. The scan loops until it gets a short page
// (< FUNDED_IDS_PAGE_SIZE rows), meaning the table is drained. A hard
// FUNDED_IDS_MAX_PAGES bound prevents a pathological loop; hitting it warns
// loudly (pool degrades by missing some funded IDs, but no unfunded company can
// sneak in — an incomplete funded set can only wrongly EXCLUDE funded companies
// from the pool, never admit unfunded ones).
const FUNDED_IDS_PAGE_SIZE = 1000;
const FUNDED_IDS_MAX_PAGES = 50;

const MS_PER_DAY = 86_400_000;

// ─── Types ────────────────────────────────────────────────────────────────────

/** One deck entry, display-ready. `facts` omits anything unknown — the deck
 * renders nothing rather than "—" (spec §2). */
export interface Spotlight {
  slug: string;
  name: string;
  oneLiner: string;
  facts: string[];
}

interface RecentRound {
  company_id: string;
  round_type: string | null;
  amount_raised: number | null;
  announced_date: string;
}

interface CompanyScore {
  score: number;
  latestRoundDate: string | null;
  latestRoundType: string | null;
}

interface SpotlightCompanyRow {
  id: string;
  slug: string;
  name: string;
  description_short: string;
  hq_city: string | null;
  hq_state: string | null;
  employee_count_min: number | null;
  employee_count_max: number | null;
}

// ─── Pure functions ───────────────────────────────────────────────────────────

/** Deterministic PRNG (mulberry32) — good enough to shuffle ten items. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), a | 1);
    t = (t + Math.imul(t ^ (t >>> 7), t | 61)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** UTC date as YYYYMMDD — the daily shuffle seed. With revalidate = 21600 the
 * order flips within ≤6h of midnight UTC; accepted behavior per spec. */
export function utcDateSeed(now: Date): number {
  return (
    now.getUTCFullYear() * 10_000 +
    (now.getUTCMonth() + 1) * 100 +
    now.getUTCDate()
  );
}

/** Fisher–Yates with a seeded PRNG. Returns a new array. */
export function seededShuffle<T>(items: readonly T[], seed: number): T[] {
  const rand = mulberry32(seed);
  const out = [...items];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

/**
 * Score every company that appears in the activity windows.
 *
 *   funding   = max over its recent rounds of recency + amount bonus
 *   news      = 0.5 × min(articles_30d, 10)
 *   freshness = 1.5 if created within 30d
 *
 * Also tracks each company's most recent round (date + type) for tie-breaking
 * and the deck's facts row.
 */
export function scoreCompanies(
  rounds: readonly RecentRound[],
  newsCounts: ReadonlyMap<string, number>,
  freshIds: ReadonlySet<string>,
  now: Date,
): Map<string, CompanyScore> {
  const byCompany = new Map<string, CompanyScore>();

  for (const round of rounds) {
    const days = Math.max(
      0,
      (now.getTime() - Date.parse(`${round.announced_date}T00:00:00Z`)) /
        MS_PER_DAY,
    );
    const recency =
      FUNDING_RECENCY_WEIGHT * Math.max(0, 1 - days / FUNDING_WINDOW_DAYS);
    const amountBonus =
      round.amount_raised != null && round.amount_raised > 0
        ? Math.log10(round.amount_raised) / AMOUNT_LOG_DIVISOR
        : 0;
    const fundingScore = recency + amountBonus;

    const existing = byCompany.get(round.company_id);
    if (!existing) {
      byCompany.set(round.company_id, {
        score: fundingScore,
        latestRoundDate: round.announced_date,
        latestRoundType: round.round_type,
      });
    } else {
      existing.score = Math.max(existing.score, fundingScore);
      if (
        existing.latestRoundDate === null ||
        round.announced_date > existing.latestRoundDate
      ) {
        existing.latestRoundDate = round.announced_date;
        existing.latestRoundType = round.round_type;
      }
    }
  }

  const allIds = new Set([
    ...byCompany.keys(),
    ...newsCounts.keys(),
    ...freshIds,
  ]);
  for (const id of allIds) {
    const entry = byCompany.get(id) ?? {
      score: 0,
      latestRoundDate: null,
      latestRoundType: null,
    };
    entry.score +=
      NEWS_WEIGHT * Math.min(newsCounts.get(id) ?? 0, NEWS_COUNT_CAP);
    if (freshIds.has(id)) entry.score += FRESHNESS_BONUS;
    byCompany.set(id, entry);
  }

  return byCompany;
}

/** Most-recent-first compare for ISO dates, nulls last. */
function compareRecency(a: string | null, b: string | null): number {
  if (a === b) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return b.localeCompare(a);
}

function toSpotlight(
  row: SpotlightCompanyRow,
  score: CompanyScore | undefined,
): Spotlight {
  const facts: string[] = [];
  if (score?.latestRoundType) facts.push(score.latestRoundType);
  if (row.hq_city || row.hq_state) {
    facts.push(formatLocation(row.hq_city, row.hq_state));
  }
  if (row.employee_count_min != null || row.employee_count_max != null) {
    facts.push(
      `${formatEmployeeRange(row.employee_count_min, row.employee_count_max)} people`,
    );
  }
  return {
    slug: row.slug,
    name: row.name,
    oneLiner: row.description_short,
    facts,
  };
}

// ─── Pool builder ─────────────────────────────────────────────────────────────

const SPOTLIGHT_COLUMNS =
  "id, slug, name, description_short, hq_city, hq_state, employee_count_min, employee_count_max";

/**
 * Keyset-scan `funding_rounds` and return the full set of company IDs that have
 * at least one round on record — the credibility gate for "Today's spotlight".
 *
 * PostgREST hard-caps every response at {@link FUNDED_IDS_PAGE_SIZE} rows, so
 * we paginate via `.gt("id", cursor)` until we get a short page (table drained)
 * or hit {@link FUNDED_IDS_MAX_PAGES} (warn and return what we have). The max
 * scan capacity is `{@link FUNDED_IDS_MAX_PAGES} × {@link FUNDED_IDS_PAGE_SIZE}`
 * rows; raise `FUNDED_IDS_MAX_PAGES` if `funding_rounds` grows past that.
 *
 * Error semantics: any page error → `console.error` → return the IDs collected
 * so far (or empty set). An incomplete set can only wrongly EXCLUDE funded
 * companies — it cannot admit unfunded ones — so returning early is correct.
 */
async function collectFundedCompanyIds(
  supabase: ReturnType<typeof createSupabaseServerClient>,
): Promise<Set<string>> {
  const fundedIds = new Set<string>();
  let lastId: string | null = null;

  for (let page = 0; page < FUNDED_IDS_MAX_PAGES; page++) {
    let q = supabase
      .from("funding_rounds")
      .select("id, company_id")
      .order("id", { ascending: true })
      .limit(FUNDED_IDS_PAGE_SIZE);
    if (lastId !== null) {
      q = q.gt("id", lastId);
    }

    const { data, error } = await q;

    if (error) {
      console.error(
        "[buildSpotlightPool] funded-ids page query failed:",
        error.message,
      );
      return fundedIds;
    }

    const rows = (data ?? []) as { id: string; company_id: string }[];
    for (const r of rows) fundedIds.add(r.company_id);

    // Short page → table drained.
    if (rows.length < FUNDED_IDS_PAGE_SIZE) return fundedIds;

    lastId = rows[rows.length - 1].id;
  }

  console.warn(
    `[buildSpotlightPool] funded-ids scan hit maxPages=${FUNDED_IDS_MAX_PAGES} ` +
      `(capacity ${FUNDED_IDS_MAX_PAGES * FUNDED_IDS_PAGE_SIZE} rows; ` +
      `${fundedIds.size} unique funded companies collected so far); ` +
      "some funded companies may be missing from the spotlight pool — " +
      "raise FUNDED_IDS_MAX_PAGES if the funding_rounds table grows past " +
      `${FUNDED_IDS_MAX_PAGES * FUNDED_IDS_PAGE_SIZE} rows.`,
  );
  return fundedIds;
}

/**
 * Build the daily pool of up to 10 spotlights: score recent activity, keep the
 * top eligible companies (description_short present, status active, ≥1 funding
 * round on record), fill from newest eligible companies if short, then shuffle
 * with today's UTC-date seed.
 *
 * Returns `[]` in three cases — the page renders its fallback in all of them:
 * - Supabase is unconfigured (env vars absent).
 * - The index is empty (no companies yet).
 * - The funded-ids scan fails entirely (returns an empty set → no company
 *   passes the funding gate → empty pool). This is deliberate: integrity over
 *   availability for the funding rule. The older behavior was partial
 *   degradation (serve whatever scored highest, ignoring the gate); that could
 *   surface unfunded companies on the front page during an outage, which
 *   violates the credibility bar. Fail-closed is the correct trade-off.
 */
export async function buildSpotlightPool(
  now: Date = new Date(),
): Promise<Spotlight[]> {
  let supabase: ReturnType<typeof createSupabaseServerClient>;
  try {
    supabase = createSupabaseServerClient();
  } catch (err) {
    console.warn(
      "[buildSpotlightPool] Supabase not configured:",
      (err as Error).message,
    );
    return [];
  }

  const fundingSince = new Date(
    now.getTime() - FUNDING_WINDOW_DAYS * MS_PER_DAY,
  )
    .toISOString()
    .slice(0, 10);
  const newsSince = new Date(now.getTime() - NEWS_WINDOW_DAYS * MS_PER_DAY)
    .toISOString()
    .slice(0, 10);
  const freshSince = new Date(
    now.getTime() - FRESHNESS_WINDOW_DAYS * MS_PER_DAY,
  ).toISOString();

  // Fan out all four independent queries in parallel: the funded-ids scan is
  // independent of the three scoring queries, so folding it into Promise.all
  // removes a serial round-trip (it was previously awaited before the others).
  //
  // Post-fetch Set filter (rather than an .in() on candidateIds) keeps this
  // module's pattern: compute everything in-process rather than pushing
  // complex filter logic onto the query planner for a small data set.
  const [fundedIds, roundsResult, newsResult, freshResult] = await Promise.all([
    collectFundedCompanyIds(supabase),
    supabase
      .from("funding_rounds")
      .select("company_id, round_type, amount_raised, announced_date")
      .gte("announced_date", fundingSince),
    supabase
      .from("news_articles")
      .select("company_id")
      .not("company_id", "is", null)
      .gte("published_date", newsSince),
    supabase.from("companies").select("id").gte("created_at", freshSince),
  ]);

  if (roundsResult.error) {
    console.error(
      "[buildSpotlightPool] funding_rounds query failed:",
      roundsResult.error.message,
    );
  }
  if (newsResult.error) {
    console.error(
      "[buildSpotlightPool] news_articles query failed:",
      newsResult.error.message,
    );
  }
  if (freshResult.error) {
    console.error(
      "[buildSpotlightPool] companies query failed:",
      freshResult.error.message,
    );
  }

  const rounds = ((roundsResult.data ?? []) as RecentRound[]).filter(
    (r) => r.announced_date !== null,
  );

  const newsCounts = new Map<string, number>();
  for (const row of (newsResult.data ?? []) as { company_id: string }[]) {
    newsCounts.set(row.company_id, (newsCounts.get(row.company_id) ?? 0) + 1);
  }

  const freshIds = new Set(
    ((freshResult.data ?? []) as { id: string }[]).map((r) => r.id),
  );

  const scores = scoreCompanies(rounds, newsCounts, freshIds, now);

  // Pre-rank by score to bound the display fetch; the full tie-break (score,
  // latest funding date, name) happens once names are known.
  const candidateIds = [...scores.entries()]
    .sort(
      (a, b) =>
        b[1].score - a[1].score ||
        compareRecency(a[1].latestRoundDate, b[1].latestRoundDate),
    )
    .slice(0, CANDIDATE_LIMIT)
    .map(([id]) => id);

  const picked: SpotlightCompanyRow[] = [];

  if (candidateIds.length > 0) {
    const { data, error } = await supabase
      .from("companies")
      .select(SPOTLIGHT_COLUMNS)
      .in("id", candidateIds)
      .not("description_short", "is", null)
      // A shut-down/acquired company must never be "Today's spotlight" —
      // exits can still score (their acquisition coverage counts as news).
      .eq("status", "active");
    if (error) {
      console.error(
        "[buildSpotlightPool] candidate fetch failed:",
        error.message,
      );
    }
    // Spotlighted companies must have at least one funding round on record —
    // funding is the credibility bar for the front page. Filter here
    // (post-fetch, in-process) to stay consistent with this file's style of
    // keeping query logic simple and doing light filtering in JS.
    const rows = ((data ?? []) as unknown as SpotlightCompanyRow[]).filter(
      (r) => fundedIds.has(r.id),
    );
    rows.sort((a, b) => {
      const sa = scores.get(a.id);
      const sb = scores.get(b.id);
      return (
        (sb?.score ?? 0) - (sa?.score ?? 0) ||
        compareRecency(
          sa?.latestRoundDate ?? null,
          sb?.latestRoundDate ?? null,
        ) ||
        a.name.localeCompare(b.name, "en-US")
      );
    });
    picked.push(...rows.slice(0, POOL_SIZE));
  }

  // Fewer than 10 qualified — fill from the most recently created companies
  // that have a description, de-duplicated against the activity picks.
  // Over-fetch to account for the funding gate (rows without a round are
  // dropped after the fetch, same as in the candidate path).
  if (picked.length < POOL_SIZE) {
    const { data, error } = await supabase
      .from("companies")
      .select(SPOTLIGHT_COLUMNS)
      .not("description_short", "is", null)
      // Same active-only rule as the candidate fetch — the fill path feeds
      // the identical spotlight deck.
      .eq("status", "active")
      .order("created_at", { ascending: false })
      .limit(POOL_SIZE * 4);
    if (error) {
      console.error("[buildSpotlightPool] fill fetch failed:", error.message);
    }
    const pickedIds = new Set(picked.map((r) => r.id));
    for (const row of (data ?? []) as unknown as SpotlightCompanyRow[]) {
      if (picked.length >= POOL_SIZE) break;
      // Spotlighted companies must have at least one funding round on record —
      // funding is the credibility bar for the front page.
      if (!pickedIds.has(row.id) && fundedIds.has(row.id)) {
        picked.push(row);
        pickedIds.add(row.id);
      }
    }
  }

  return seededShuffle(picked, utcDateSeed(now)).map((row) =>
    toSpotlight(row, scores.get(row.id)),
  );
}
