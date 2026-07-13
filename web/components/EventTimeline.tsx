// Server component — the unified event timeline on /c/[slug]. Merges the
// company's funding rounds and news articles into one reverse-chronological
// list, REPLACING the old separate FundingHistory table + News list (the two
// are never rendered alongside this — no duplication). Funding entries keep
// their full detail (round type, amount, post-money valuation, lead/other
// investors, low-confidence flag); news entries link out to the source article.
// Read-only display, all data flows in via props.
//
// Undated events can't be placed on the time axis, so they sort to the end
// (never dropped — no data loss); dated events are newest-first.

import { formatDate, formatUsd, formatUsdExact } from "@/lib/format";
import type { FundingRoundWithInvestors, NewsArticleRow } from "@/lib/types";

const EM_DASH = "—";

function joinNames(names: string[]): string {
  return names.length > 0 ? names.join(", ") : EM_DASH;
}

/** First three names, then "and N more" — matches the old FundingHistory table. */
function joinOthers(names: string[]): string {
  if (names.length === 0) return EM_DASH;
  if (names.length <= 3) return names.join(", ");
  return `${names.slice(0, 3).join(", ")} and ${names.length - 3} more`;
}

type TimelineEvent =
  | { kind: "funding"; date: string | null; round: FundingRoundWithInvestors }
  | { kind: "news"; date: string | null; article: NewsArticleRow };

interface Props {
  rounds: FundingRoundWithInvestors[];
  news: NewsArticleRow[];
}

export function EventTimeline({ rounds, news }: Props) {
  const events: TimelineEvent[] = [
    ...rounds.map(
      (round): TimelineEvent => ({
        kind: "funding",
        date: round.announced_date,
        round,
      }),
    ),
    ...news.map(
      (article): TimelineEvent => ({
        kind: "news",
        date: article.published_date,
        article,
      }),
    ),
  ].sort((a, b) => (b.date ?? "").localeCompare(a.date ?? ""));

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Timeline</h2>

      {events.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No funding rounds or news recorded yet.
        </p>
      ) : (
        <ol className="relative ml-2 border-l border-edge">
          {events.map((event) => {
            const dateLabel = event.date ? formatDate(event.date) : EM_DASH;
            const key =
              event.kind === "funding"
                ? `f-${event.round.id}`
                : `n-${event.article.id}`;
            return (
              <li key={key} className="relative pb-6 pl-6 last:pb-0">
                {/* Rail marker — money-green for funding, muted for news. */}
                <span
                  aria-hidden
                  className={`absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-canvas ${
                    event.kind === "funding" ? "bg-money" : "bg-ink-faint"
                  }`}
                />
                <p className="font-mono text-xs text-ink-faint">
                  {dateLabel} · {event.kind === "funding" ? "Funding" : "News"}
                </p>

                {event.kind === "funding" ? (
                  <FundingEntry round={event.round} />
                ) : (
                  <NewsEntry article={event.article} />
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function FundingEntry({ round }: { round: FundingRoundWithInvestors }) {
  const hasInvestors =
    round.leadInvestors.length > 0 || round.otherInvestors.length > 0;
  return (
    <>
      <p className="mt-1 flex flex-wrap items-baseline gap-x-2">
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
          <span className="font-mono text-xs text-ink-muted">
            <span title={formatUsdExact(round.valuation_post_money)}>
              {formatUsd(round.valuation_post_money)}
            </span>{" "}
            post-money
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
      </p>
      {hasInvestors && (
        <p className="mt-0.5 text-sm text-ink-soft">
          Led by {joinNames(round.leadInvestors)}
          {round.otherInvestors.length > 0 && (
            <> · {joinOthers(round.otherInvestors)}</>
          )}
        </p>
      )}
    </>
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
