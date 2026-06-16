// Server component — renders the M3 funding history table on /c/[slug].
// No "use client": this is read-only display, all data flows in via props.
//
// Syntax-highlighting metaphor (spec §3): amounts are "number literals" —
// money green in Geist Mono; dates are "comments" — muted mono.

import { formatDate, formatUsd, formatUsdExact } from "@/lib/format";
import type { FundingRoundWithInvestors } from "@/lib/types";

const EM_DASH = "—";

function joinNames(names: string[]): string {
  return names.length > 0 ? names.join(", ") : EM_DASH;
}

function joinOthers(names: string[]): string {
  if (names.length === 0) return EM_DASH;
  if (names.length <= 3) return names.join(", ");
  const first = names.slice(0, 3).join(", ");
  const remaining = names.length - 3;
  return `${first} and ${remaining} more`;
}

interface Props {
  rounds: FundingRoundWithInvestors[];
  /** ISO date of the most recent round shown, for the section freshness rider.
   *  Omitted/null when no round carries an announced date — the rider hides. */
  asOf?: string | null;
}

export function FundingHistory({ rounds, asOf }: Props) {
  return (
    <section className="mb-12">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-lg font-semibold text-ink">Funding History</h2>
        {asOf && (
          <p className="font-mono text-xs text-ink-faint">
            latest round {formatDate(asOf)}
          </p>
        )}
      </div>

      {rounds.length === 0 ? (
        <p className="text-sm text-ink-muted">No funding rounds recorded yet.</p>
      ) : (
        <div className="overflow-x-auto -mx-6 px-6">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-edge text-left text-ink-muted">
                <th className="py-2 pr-6 font-medium">Date</th>
                <th className="py-2 pr-6 font-medium">Round</th>
                <th className="py-2 pr-6 font-medium text-right">Amount</th>
                <th className="py-2 pr-6 font-medium text-right">Valuation</th>
                <th className="py-2 pr-6 font-medium">Lead</th>
                <th className="py-2 font-medium">Other investors</th>
              </tr>
            </thead>
            <tbody>
              {rounds.map((round) => {
                return (
                  <tr
                    key={round.id}
                    className="border-b border-edge hover:bg-edge/30 align-top"
                  >
                    <td className="py-3 pr-6 font-mono text-ink-muted">
                      {round.announced_date
                        ? formatDate(round.announced_date)
                        : EM_DASH}
                    </td>
                    <td className="py-3 pr-6 text-ink-soft">
                      {round.round_type ?? EM_DASH}
                      {round.extraction_confidence === "low" && (
                        <span
                          className="ml-2 inline-block rounded border border-warn px-1.5 py-0.5 text-xs text-warn align-middle"
                          title="Extracted with low confidence — treat as unverified"
                        >
                          low confidence
                        </span>
                      )}
                    </td>
                    <td className="py-3 pr-6 text-right font-mono">
                      {round.amount_raised != null ? (
                        // title shows the exact dollars — the short form rounds
                        // (e.g. $1.51M and $1.49M both display "$1.5M").
                        <span
                          className="text-money"
                          title={formatUsdExact(round.amount_raised)}
                        >
                          {formatUsd(round.amount_raised)}
                        </span>
                      ) : (
                        <span className="text-ink-faint">{EM_DASH}</span>
                      )}
                    </td>
                    <td className="py-3 pr-6 text-right">
                      {round.valuation_post_money != null ? (
                        <div className="font-mono text-money">
                          <span title={formatUsdExact(round.valuation_post_money)}>
                            {formatUsd(round.valuation_post_money)}
                          </span>
                          <span className="ml-1 font-sans text-xs text-ink-muted">
                            (post-money)
                          </span>
                        </div>
                      ) : (
                        <span className="text-ink-faint">{EM_DASH}</span>
                      )}
                    </td>
                    <td className="py-3 pr-6 text-ink-soft">
                      {joinNames(round.leadInvestors)}
                    </td>
                    <td className="py-3 text-ink-soft">
                      {joinOthers(round.otherInvestors)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
