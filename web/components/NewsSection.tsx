// Server component — the "In the news" list on /c/[slug]: STANDALONE story
// clusters only (split out of the old merged EventTimeline per the
// owner-approved 2026-07-18 design). An article that covers a specific round
// is evidence for that round and stays collapsed under it in FundingTimeline —
// every article appears exactly once. What lands here is the residue
// `buildTimeline` attaches to no round: rumors, IPO chatter, pre-announcement
// coverage (each cluster one story, syndications collapsed).
//
// Deliberately muted/compact relative to the funding rail (plain list rows, no
// rail) so the funding structure stays the page's spine. Newest 8 stories are
// visible; older ones collapse into a native <details> (server-component-safe,
// keyboard operable) — nothing is ever dropped, the trust invariant. Ordering
// is buildTimeline's, preserved by the split: dated stories newest-first,
// undated trail. Omits entirely when there are no standalone stories; the page
// owns the both-empty line.

import { formatDate } from "@/lib/format";
import type { TimelineItem } from "@/lib/timeline";
import { CoverageDisclosure } from "@/components/CoverageDisclosure";

/** A `buildTimeline` item narrowed to a standalone story — what the page
 *  passes after splitting the timeline by kind. */
export type NewsItem = Extract<TimelineItem, { kind: "news" }>;

/** Stories shown before the rest collapse behind "Show N older stories". */
export const NEWS_VISIBLE_COUNT = 8;

export function NewsSection({ items }: { items: NewsItem[] }) {
  if (items.length === 0) return null;

  const visible = items.slice(0, NEWS_VISIBLE_COUNT);
  const older = items.slice(NEWS_VISIBLE_COUNT);

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">In the news</h2>

      <ul className="flex flex-col gap-4">
        {visible.map((item) => (
          <StoryRow key={item.article.id} item={item} />
        ))}
      </ul>

      {older.length > 0 && (
        <details className="group mt-4">
          <summary className="flex w-fit cursor-pointer list-none items-center gap-1.5 text-sm text-ink-muted hover:text-ink [&::-webkit-details-marker]:hidden">
            <svg
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              aria-hidden
              className="h-3 w-3 shrink-0 text-ink-faint transition-transform group-open:rotate-90"
            >
              <path
                d="M7 4l7 6-7 6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            <span>
              Show {older.length} older stor{older.length === 1 ? "y" : "ies"}
            </span>
          </summary>
          <ul className="mt-4 flex flex-col gap-4">
            {older.map((item) => (
              <StoryRow key={item.article.id} item={item} />
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

/** One story: the lead article's headline links out; date + source host
 *  beneath; syndicated copies (coverage ≥2) collapse into the SAME "Covered
 *  by" disclosure round coverage uses. */
function StoryRow({ item }: { item: NewsItem }) {
  const { article, coverage } = item;
  // The lead's host, as the lib computed it (coverage is lead-first and only
  // ever holds renderable URLs, so [0] exists whenever the row does).
  const host = coverage[0]?.host ?? null;
  return (
    <li>
      <a
        href={article.url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-sm font-medium text-ink-soft underline-offset-2 hover:text-ink hover:underline"
      >
        {article.title}
      </a>
      {(article.published_date || host) && (
        <p className="mt-0.5 font-mono text-xs text-ink-muted">
          {article.published_date && formatDate(article.published_date)}
          {article.published_date && host && " · "}
          {host}
        </p>
      )}
      {coverage.length >= 2 && <CoverageDisclosure coverage={coverage} />}
    </li>
  );
}
