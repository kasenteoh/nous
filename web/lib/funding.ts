// Shared "total raised" derivation (W-C.4). Three surfaces render a total —
// the company-page tile, the OG card (getCompanyOgData), and the compare table
// (getCompaniesForCompare) — and they must agree.
//
// Invariant: totalRaised = max(stated cumulative total, sum of round amounts
// DE-DUPLICATED on (round_type, amount_raised)).
//
// Why the dedup: the historical news backfill could re-report ONE round from
// several articles (same amount, often a null round_type), and a naive sum
// multiplies it — Helion's $465M Series G summed to $2.3B across 5 rows. The
// pipeline fix (reconcile + repair-duplicate-rounds) is the primary cure; the
// dedup here means a stray dupe that slips through can't inflate the total.
// Distinct rounds that genuinely share an amount keep different round_types,
// so they still both count; null-amount rounds contribute nothing either way.
//
// Why the max: a stated total ("has raised $285M to date") usually covers
// early rounds news coverage missed, while the summed rounds can exceed a
// stale stated figure after a new raise — whichever is larger is the better
// floor. Callers needing the citation use `statedWins` to pick the source.
//
// Import-safe anywhere (no imports, no env access).

export interface RoundAmount {
  round_type?: string | null;
  amount_raised: number | string | null;
}

/** Sum of round amounts, de-duplicated on (round_type, amount_raised). */
export function dedupedRoundsTotal(rounds: readonly RoundAmount[]): number {
  const seen = new Set<string>();
  let total = 0;
  for (const r of rounds) {
    if (r.amount_raised == null) continue;
    const key = `${r.round_type ?? ""}::${r.amount_raised}`;
    if (seen.has(key)) continue;
    seen.add(key);
    total += Number(r.amount_raised);
  }
  return total;
}

export interface TotalRaised {
  /** max(stated, deduped round sum) — the figure every surface renders. */
  total: number;
  /** True when the stated figure is the one shown (drives the citation). */
  statedWins: boolean;
  /** True when at least one round carries an amount. */
  hasComputed: boolean;
  /** True when there is anything to show at all. */
  hasTotal: boolean;
}

export function computeTotalRaised(
  statedTotal: number | string | null | undefined,
  rounds: readonly RoundAmount[],
): TotalRaised {
  const computed = dedupedRoundsTotal(rounds);
  const stated = statedTotal != null ? Number(statedTotal) : null;
  const statedWins = stated != null && stated >= computed;
  const hasComputed = rounds.some((r) => r.amount_raised != null);
  return {
    total: statedWins ? (stated as number) : computed,
    statedWins,
    hasComputed,
    hasTotal: hasComputed || stated != null,
  };
}

// ─── Funding by quarter (themes, Wave 3 E-3) ──────────────────────────────────

export interface DatedRoundAmount {
  announced_date: string | null; // ISO date (YYYY-MM-DD) or null
  amount_raised: number | string | null;
}

export interface QuarterBucket {
  /** Display label, e.g. "Q3 2025". */
  label: string;
  /** ISO date of the quarter's first day — a stable key for rendering. */
  start: string;
  /** Sum of round amounts announced in this quarter, USD. */
  totalUsd: number;
}

/** First month (1-based) of the calendar quarter containing `month`. */
function quarterFirstMonth(month: number): number {
  return 3 * Math.floor((month - 1) / 3) + 1;
}

/**
 * Bucket funding rounds into the `quarters` most recent calendar quarters
 * (oldest first), INCLUDING the in-progress current quarter — this is a
 * display series, so recent activity should show; the theme row's growth
 * metric separately compares only complete quarters (see the pipeline's
 * compute-themes stage). Quarters with no dated rounds appear with a 0 so
 * the bar chart's time axis has no silent gaps. Rounds without a date or an
 * amount contribute nothing (they cannot be placed — unknown stays unknown).
 *
 * `now` is injectable for tests; date math is done on the ISO string's
 * year/month so time zones can't shift a round across a quarter boundary.
 */
export function bucketFundingByQuarter(
  rounds: readonly DatedRoundAmount[],
  quarters = 8,
  now: Date = new Date(),
): QuarterBucket[] {
  // Build the window of (year, firstMonth) quarter keys, oldest first.
  let year = now.getUTCFullYear();
  let month = quarterFirstMonth(now.getUTCMonth() + 1);
  const keys: { year: number; month: number }[] = [];
  for (let i = 0; i < quarters; i++) {
    keys.unshift({ year, month });
    month -= 3;
    if (month < 1) {
      month += 12;
      year -= 1;
    }
  }

  const totals = new Map<string, number>();
  const keyOf = (y: number, m: number): string =>
    `${y}-${String(m).padStart(2, "0")}`;
  for (const k of keys) totals.set(keyOf(k.year, k.month), 0);

  for (const round of rounds) {
    if (round.announced_date == null || round.amount_raised == null) continue;
    const match = /^(\d{4})-(\d{2})/.exec(round.announced_date);
    if (!match) continue;
    const key = keyOf(Number(match[1]), quarterFirstMonth(Number(match[2])));
    if (!totals.has(key)) continue; // outside the window
    totals.set(key, (totals.get(key) ?? 0) + Number(round.amount_raised));
  }

  return keys.map((k) => ({
    label: `Q${Math.floor((k.month - 1) / 3) + 1} ${k.year}`,
    start: `${keyOf(k.year, k.month)}-01`,
    totalUsd: totals.get(keyOf(k.year, k.month)) ?? 0,
  }));
}
