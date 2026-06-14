// Server component — renders the News section on /c/[slug].
// No "use client": read-only display, all data flows in via props. Each item
// links out to the original article; date/source are shown as a muted subline.

import { formatDate } from "@/lib/format";
import type { NewsArticleRow } from "@/lib/types";

interface Props {
  news: NewsArticleRow[];
  /** ISO date of the most recent article shown, for the section freshness rider.
   *  Omitted/null when no article carries a published date — the rider hides. */
  asOf?: string | null;
}

export function News({ news, asOf }: Props) {
  return (
    <section className="mb-12">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-lg font-semibold text-ink">News</h2>
        {asOf && (
          <p className="font-mono text-xs text-ink-faint">
            latest {formatDate(asOf)}
          </p>
        )}
      </div>

      {news.length === 0 ? (
        <p className="text-sm text-ink-muted">No news yet.</p>
      ) : (
        <ul className="divide-y divide-edge">
          {news.map((article) => (
            <li key={article.id} className="py-3">
              <a
                href={article.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-ink hover:underline underline-offset-2 font-medium"
              >
                {article.title}
              </a>
              <p className="mt-1 font-mono text-xs text-ink-muted">
                {article.source}
                {article.published_date && (
                  <> · {formatDate(article.published_date)}</>
                )}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
