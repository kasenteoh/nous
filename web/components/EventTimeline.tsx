// Server component — the unified event timeline on /c/[slug]. Merges the
// company's funding rounds and news into one reverse-chronological list
// (REPLACING the old separate FundingHistory table + News list). Because
// ingest-news only ingests funding announcements, the "news" IS the funding
// coverage — so a well-covered round would otherwise render as N near-duplicate
// news rows. `buildTimeline` (lib/timeline.ts) clusters each article UNDER the
// round it covers; a round with ≥2 sources shows a collapsed "Covered by …"
// disclosure (every article one click away — the moat intact), while lightly
// covered rounds keep their single inline source. Read-only display.
//
// Ordering (in buildTimeline): dated events run newest-first; undated FUNDING
// leads (the structured spine — an undated $65B round must not sink below dated
// news), undated news trails.

import { formatDate, formatUsd, formatUsdExact } from "@/lib/format";
import type {
  FactVerification,
  FundingRoundWithInvestors,
  NewsArticleRow,
} from "@/lib/types";
import { buildTimeline, type CoverageLink } from "@/lib/timeline";
import { SourceLink } from "@/components/SourceLink";
import { VerifiedBadge } from "@/components/VerifiedBadge";
import { verifiedAgainst } from "@/lib/verifications";

const EM_DASH = "—";

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
  rounds: FundingRoundWithInvestors[];
  news: NewsArticleRow[];
  /** `supported` source-verifications keyed by (fact_kind, fact_ref) — a round's
   *  ✓ shows when its id + current source match. Absent → no badges. */
  verified?: Map<string, FactVerification>;
}

export function EventTimeline({ rounds, news, verified }: Props) {
  const items = buildTimeline(rounds, news);

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Timeline</h2>

      {items.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No funding rounds or news recorded yet.
        </p>
      ) : (
        <ol
          className="relative ml-2 border-l border-edge"
          aria-label="Company timeline"
        >
          {items.map((item) => {
            const isFunding = item.kind === "funding";
            const date = isFunding
              ? item.round.announced_date
              : item.article.published_date;
            const dateLabel = date ? formatDate(date) : EM_DASH;
            const key = isFunding
              ? `f-${item.round.id}`
              : `n-${item.article.id}`;
            return (
              <li key={key} className="relative pb-6 pl-6 last:pb-0">
                {/* Rail marker — money-green for funding, muted for news. */}
                <span
                  aria-hidden
                  className={`absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-canvas ${
                    isFunding ? "bg-money" : "bg-ink-faint"
                  }`}
                />
                <p className="font-mono text-xs text-ink-muted">
                  {dateLabel} · {isFunding ? "Funding" : "News"}
                </p>

                {item.kind === "funding" ? (
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
                          )
                        : null
                    }
                  />
                ) : (
                  <NewsEntry article={item.article} />
                )}
              </li>
            );
          })}
        </ol>
      )}
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

/**
 * A round's press coverage, collapsed. Native <details> (server-component-safe,
 * keyboard-operable, exposes open state to assistive tech for free): the summary
 * names the first two hosts + a "+N more sources" count; expanding lists every
 * article as a source link. Trust-preserving — every source is one click away,
 * never dropped.
 */
function CoverageDisclosure({ coverage }: { coverage: CoverageLink[] }) {
  // Name DISTINCT outlets (two URLs from one outlet must not read "Covered by
  // techcrunch.com, techcrunch.com"); the count is remaining distinct outlets.
  const outlets = [...new Set(coverage.map((c) => c.host))];
  const shown = outlets.slice(0, 2);
  const extra = outlets.length - shown.length;
  return (
    <details className="group mt-1.5">
      <summary className="flex w-fit cursor-pointer list-none items-center gap-1.5 text-sm text-ink-muted hover:text-ink [&::-webkit-details-marker]:hidden">
        <svg
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          aria-hidden
          className="h-3 w-3 shrink-0 text-ink-faint transition-transform group-open:rotate-90"
        >
          <path d="M7 4l7 6-7 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <span>
          Covered by {shown.join(", ")}
          {extra > 0 && (
            <span className="text-ink-muted">
              {" "}
              +{extra} more source{extra === 1 ? "" : "s"}
            </span>
          )}
        </span>
      </summary>
      <ul className="mt-2 ml-[18px] flex flex-col gap-1.5">
        {coverage.map((c) => (
          <li key={c.url} className="text-sm leading-snug">
            <a
              href={c.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-ink-muted underline-offset-2 hover:text-ink hover:underline"
            >
              {c.title ?? c.host}
            </a>
            {c.title && (
              <span className="ml-1.5 text-xs text-ink-muted">· {c.host}</span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}

function NewsEntry({ article }: { article: NewsArticleRow }) {
  return (
    <>
      <a
        href={article.url}
        target="_blank"
        rel="noopener noreferrer"
        className="mt-1 inline-block font-medium text-ink hover:underline underline-offset-2"
      >
        {article.title}
      </a>
      {article.source && (
        <p className="mt-0.5 font-mono text-xs text-ink-muted">
          {article.source}
        </p>
      )}
    </>
  );
}
