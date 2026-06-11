// Server component — renders the News section on /c/[slug].
// No "use client": read-only display, all data flows in via props. Each item
// links out to the original article; date/source are shown as a muted subline.

import { formatDate } from "@/lib/format";
import type { NewsArticleRow } from "@/lib/types";

interface Props {
  news: NewsArticleRow[];
}

export function News({ news }: Props) {
  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-4">
        News
      </h2>

      {news.length === 0 ? (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">No news yet.</p>
      ) : (
        <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {news.map((article) => (
            <li key={article.id} className="py-3">
              <a
                href={article.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-zinc-900 dark:text-zinc-100 hover:underline underline-offset-2 font-medium"
              >
                {article.title}
              </a>
              <p className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
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
