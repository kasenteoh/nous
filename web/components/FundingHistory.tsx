// Server component — renders the M3 funding history table on /c/[slug].
// No "use client": this is read-only display, all data flows in via props.

import { formatDate, formatUsd } from "@/lib/format";
import type { FundingRoundWithInvestors } from "@/lib/types";

// Inline 3-line URL → hostname helper. Kept local rather than in format.ts
// because nothing else in the app needs it yet (and per Chunk-7 scope).
function hostname(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url).host.toLowerCase();
  } catch {
    return null;
  }
}

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
}

export function FundingHistory({ rounds }: Props) {
  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-4">
        Funding History
      </h2>

      {rounds.length === 0 ? (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">
          No funding rounds recorded yet.
        </p>
      ) : (
        <div className="overflow-x-auto -mx-6 px-6">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-zinc-200 dark:border-zinc-700 text-left text-zinc-500 dark:text-zinc-400">
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
                const sourceHost = hostname(round.primary_news_url);
                return (
                  <tr
                    key={round.id}
                    className="border-b border-zinc-100 dark:border-zinc-800 hover:bg-zinc-50 dark:hover:bg-zinc-900/50 align-top"
                  >
                    <td className="py-3 pr-6 text-zinc-700 dark:text-zinc-300">
                      {round.announced_date
                        ? formatDate(round.announced_date)
                        : EM_DASH}
                    </td>
                    <td className="py-3 pr-6 text-zinc-700 dark:text-zinc-300">
                      {round.round_type ?? EM_DASH}
                    </td>
                    <td className="py-3 pr-6 text-right text-zinc-700 dark:text-zinc-300">
                      {round.amount_raised != null
                        ? formatUsd(round.amount_raised)
                        : EM_DASH}
                    </td>
                    <td className="py-3 pr-6 text-right text-zinc-700 dark:text-zinc-300">
                      {round.valuation_post_money != null ? (
                        <>
                          <div>
                            {formatUsd(round.valuation_post_money)}
                            <span className="ml-1 text-xs text-zinc-400 dark:text-zinc-500">
                              (post-money)
                            </span>
                          </div>
                          {round.valuation_source && (
                            <div className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
                              via {round.valuation_source}
                            </div>
                          )}
                        </>
                      ) : (
                        EM_DASH
                      )}
                    </td>
                    <td className="py-3 pr-6 text-zinc-700 dark:text-zinc-300">
                      <div>{joinNames(round.leadInvestors)}</div>
                      {sourceHost && round.primary_news_url && (
                        <div className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
                          via{" "}
                          <a
                            href={round.primary_news_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="underline underline-offset-2 hover:text-zinc-600 dark:hover:text-zinc-300"
                          >
                            {sourceHost}
                          </a>
                        </div>
                      )}
                    </td>
                    <td className="py-3 text-zinc-700 dark:text-zinc-300">
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
