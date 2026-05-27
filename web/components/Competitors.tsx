// Server component — renders the M4 competitors section on /c/[slug].
// No "use client": read-only display, all data flows in via props. Cards
// link internally when the competitor resolved to an indexed company.

import Link from "next/link";
import type { CompetitorWithResolved } from "@/lib/types";

interface Props {
  competitors: CompetitorWithResolved[];
}

export function Competitors({ competitors }: Props) {
  if (competitors.length === 0) {
    // Section omitted entirely when there is nothing to show — same convention
    // as the FundingHistory empty state and spec §11 (unknown = hidden).
    return null;
  }

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-4">
        Competitors
      </h2>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {competitors.map((c) => (
          <article
            key={c.id}
            className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4"
          >
            <header className="flex items-baseline gap-2">
              {c.resolved ? (
                <Link
                  href={`/c/${c.resolved.slug}`}
                  className="font-semibold text-zinc-900 dark:text-zinc-100 hover:underline underline-offset-2"
                >
                  {c.competitor_name}
                </Link>
              ) : (
                <span className="font-semibold text-zinc-900 dark:text-zinc-100">
                  {c.competitor_name}
                </span>
              )}
              <span className="ml-auto text-xs text-zinc-400 dark:text-zinc-500">
                #{c.rank}
              </span>
            </header>

            {c.description && (
              <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-300 leading-snug">
                {c.description}
              </p>
            )}

            {c.reasoning && (
              <p className="mt-2 text-xs text-zinc-400 dark:text-zinc-500 leading-snug">
                <span className="font-medium">Why they compete: </span>
                {c.reasoning}
              </p>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
