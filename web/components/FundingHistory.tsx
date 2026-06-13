// Server component — renders the M3 funding history table on /c/[slug].
// No "use client": this is read-only display, all data flows in via props.
//
// Syntax-highlighting metaphor (spec §3): amounts are "number literals" —
// money green in Geist Mono; dates are "comments" — muted mono.

import { formatDate, formatUsd } from "@/lib/format";
import type { FundingRoundWithInvestors } from "@/lib/types";

// Inline URL → hostname helper. Strips www. for consistent comparison.
// Kept local rather than in format.ts because nothing else needs it yet.
function hostname(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return null;
  }
}

/**
 * Derives a compact provenance label for a funding figure given the source URL
 * and the company's own website. Returns "Company-stated" when the source host
 * matches the company's own domain (self-reported), or "via {host}" when it
 * comes from an independent article. Returns null when no source URL exists.
 */
function provenanceLabel(
  sourceUrl: string | null | undefined,
  companyWebsite: string | null | undefined,
): { label: string; isOwn: boolean } | null {
  const srcHost = hostname(sourceUrl);
  if (!srcHost) return null;
  const coHost = hostname(companyWebsite);
  const isOwn = coHost !== null && srcHost === coHost;
  return { label: isOwn ? "Company-stated" : `via ${srcHost}`, isOwn };
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
  /** The company's own website URL — used to distinguish self-reported figures
   *  from independently-reported ones (journalism vs company IR page). */
  companyWebsite: string | null;
}

export function FundingHistory({ rounds, companyWebsite }: Props) {
  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Funding History</h2>

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
                        <span className="text-money">
                          {formatUsd(round.amount_raised)}
                        </span>
                      ) : (
                        <span className="text-ink-faint">{EM_DASH}</span>
                      )}
                    </td>
                    <td className="py-3 pr-6 text-right">
                      {round.valuation_post_money != null ? (
                        <>
                          <div className="font-mono text-money">
                            {formatUsd(round.valuation_post_money)}
                            <span className="ml-1 font-sans text-xs text-ink-muted">
                              (post-money)
                            </span>
                          </div>
                          {round.valuation_source && (
                            <div className="mt-1 text-xs text-ink-muted">
                              via {round.valuation_source}
                            </div>
                          )}
                        </>
                      ) : (
                        <span className="text-ink-faint">{EM_DASH}</span>
                      )}
                    </td>
                    <td className="py-3 pr-6 text-ink-soft">
                      <div>{joinNames(round.leadInvestors)}</div>
                      {round.primary_news_url && (() => {
                        const prov = provenanceLabel(round.primary_news_url, companyWebsite);
                        if (!prov) return null;
                        // srcHost is guaranteed non-null when prov is non-null
                        const srcHost = hostname(round.primary_news_url)!;
                        return (
                          <div className="mt-1 text-xs text-ink-muted">
                            {prov.isOwn ? (
                              <span title="Figure reported on the company's own website">
                                Company-stated
                              </span>
                            ) : (
                              <>
                                via{" "}
                                <a
                                  href={round.primary_news_url}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="underline underline-offset-2 hover:text-ink-soft"
                                >
                                  {srcHost}
                                </a>
                              </>
                            )}
                          </div>
                        );
                      })()}
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
