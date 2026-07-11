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
