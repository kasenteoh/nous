// Server component — the "Funding" rail on /c/[slug]: the company's funding
// rounds ONLY (split out of the old merged EventTimeline per the owner-approved
// 2026-07-18 design; standalone news lives in NewsSection directly beneath).
// Each round keeps everything the merged row had: the money-green rail marker,
// amount/valuation/investors, the ✓ VerifiedBadge, the extraction-confidence
// tooltip (+ low-confidence pill), a single source's inline ↗, and the
// collapsed "Covered by …" disclosure for ≥2 sources — coverage is evidence
// for the round, so a round-covering article stays HERE, never in NewsSection.
//
// Pure presentation: takes pre-split items from `buildTimeline` (the page
// calls it once and splits by kind — no double computation). Ordering is
// buildTimeline's, preserved by the split: undated funding leads (the
// structured spine), dated rounds run newest-first. Omits entirely when the
// company has no rounds; the page owns the both-empty line.

import { formatDate, formatUsd, formatUsdExact } from "@/lib/format";
import type { FactVerification, FundingRoundWithInvestors } from "@/lib/types";
import type { CoverageLink, TimelineItem } from "@/lib/timeline";
import { CoverageDisclosure } from "@/components/CoverageDisclosure";
import { SourceLink } from "@/components/SourceLink";
import { VerifiedBadge } from "@/components/VerifiedBadge";
import { verifiedAgainst } from "@/lib/verifications";

const EM_DASH = "—";

/** A `buildTimeline` item narrowed to a funding round — what the page passes
 *  after splitting the timeline by kind. */
export type FundingItem = Extract<TimelineItem, { kind: "funding" }>;

/**
 * Hover tooltip surfacing a round's extraction confidence on ALL rounds
 * (transparency without a wall of pills — only `low` keeps a visible pill, the
 * warning). Returns undefined for null/absent/unknown values so we never claim
 * a confidence we don't have.
 */
function confidenceTooltip(confidence: string | null): string | undefined {
  switch (confidence) {
    case "high":
      return "Extracted with high confidence";
    case "medium":
      return "Extracted with medium confidence";
    case "low":
      return "Extracted with low confidence";
    default:
      return undefined;
  }
}

function joinNames(names: string[]): string {
  return names.length > 0 ? names.join(", ") : EM_DASH;
}

/** First three names, then "and N more" — matches the old FundingHistory table. */
function joinOthers(names: string[]): string {
  if (names.length === 0) return EM_DASH;
  if (names.length <= 3) return names.join(", ");
  return `${names.slice(0, 3).join(", ")} and ${names.length - 3} more`;
}

interface Props {
  items: FundingItem[];
  /** `supported` source-verifications keyed by (fact_kind, fact_ref) — a round's
   *  ✓ shows when its id + current source match. Absent → no badges. */
  verified?: Map<string, FactVerification>;
}

export function FundingTimeline({ items, verified }: Props) {
  if (items.length === 0) return null;

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Funding</h2>

      <ol
        className="relative ml-2 border-l border-edge"
        aria-label="Funding rounds"
      >
        {items.map((item) => {
          const date = item.round.announced_date;
          return (
            <li key={item.round.id} className="relative pb-6 pl-6 last:pb-0">
              {/* Rail marker — money-green, the funding spine's visual. */}
              <span
                aria-hidden
                className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-canvas bg-money"
              />
              <p className="font-mono text-xs text-ink-muted">
                {date ? formatDate(date) : EM_DASH}
              </p>
              <FundingEntry
                round={item.round}
                coverage={item.coverage}
                verified={
                  verified
                    ? verifiedAgainst(
                        verified,
                        "funding_round",
                        item.round.id,
                        item.round.primary_news_url,
                        { kind: "amount", amountUsd: item.round.amount_raised },
                      )
                    : null
                }
              />
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function FundingEntry({
  round,
  coverage,
  verified,
}: {
  round: FundingRoundWithInvestors;
  coverage: CoverageLink[];
  verified?: FactVerification | null;
}) {
  const hasInvestors =
    round.leadInvestors.length > 0 || round.otherInvestors.length > 0;
  return (
    <>
      {/* title carries the extraction confidence for EVERY round (transparency);
          the visible pill below stays low-only (the warning). */}
      <p
        className="mt-1 flex flex-wrap items-baseline gap-x-2"
        title={confidenceTooltip(round.extraction_confidence)}
      >
        <span className="font-medium text-ink">
          {round.round_type ?? "Funding round"}
        </span>
        {round.amount_raised != null && (
          <span
            className="font-mono text-money"
            title={formatUsdExact(round.amount_raised)}
          >
            {formatUsd(round.amount_raised)}
          </span>
        )}
        {round.valuation_post_money != null && (
          <span className="font-mono text-xs">
            <span
              className="text-money"
              title={formatUsdExact(round.valuation_post_money)}
            >
              {formatUsd(round.valuation_post_money)}
            </span>{" "}
            <span className="text-ink-muted">post-money</span>
          </span>
        )}
        {round.extraction_confidence === "low" && (
          <span
            className="inline-block rounded border border-warn px-1.5 py-0.5 text-xs text-warn"
            title="Extracted with low confidence — treat as unverified"
          >
            low confidence
          </span>
        )}
        {/* Exactly one source → a subtle inline ↗ (self-omits on a bad URL). Two
            or more → the collapsed coverage disclosure below instead, so the
            inline ↗ isn't shown twice. */}
        {coverage.length === 1 && (
          <SourceLink url={coverage[0].url} label="Funding round" />
        )}
        {/* ✓ when this round's figure is verified against its cited source
            (supported + source-matched upstream; shows regardless of how many
            outlets covered it). */}
        <VerifiedBadge verification={verified} label="Funding round" />
      </p>
      {hasInvestors && (
        <p className="mt-0.5 text-sm text-ink-soft">
          {round.leadInvestors.length > 0 && (
            <>Led by {joinNames(round.leadInvestors)}</>
          )}
          {round.leadInvestors.length > 0 &&
            round.otherInvestors.length > 0 &&
            " · "}
          {round.otherInvestors.length > 0 && joinOthers(round.otherInvestors)}
        </p>
      )}
      {coverage.length >= 2 && <CoverageDisclosure coverage={coverage} />}
    </>
  );
}
