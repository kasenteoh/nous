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
      <h2 className="text-lg font-semibold text-ink mb-4">Competitors</h2>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {competitors.map((c) => (
          <article
            key={c.id}
            className="rounded-lg border border-edge p-4"
          >
            <header className="flex items-baseline gap-2">
              {c.resolved ? (
                <Link
                  href={`/c/${c.resolved.slug}`}
                  className="font-semibold text-ink hover:underline underline-offset-2"
                >
                  {c.competitor_name}
                </Link>
              ) : (
                <span className="font-semibold text-ink">
                  {c.competitor_name}
                </span>
              )}
              <span className="ml-auto text-xs text-ink-faint">#{c.rank}</span>
            </header>

            {/* Provenance, comment-style: TechCrunch-grounded vs LLM-inferred
                ("potential"). The wording carries the distinction; no colored
                chips in this skin. */}
            {c.source === "techcrunch" ? (
              c.source_url ? (
                <a
                  href={c.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-2 inline-block font-mono text-xs text-ink-muted underline underline-offset-2 hover:text-ink-soft"
                >
                  via TechCrunch
                </a>
              ) : (
                <span className="mt-2 inline-block font-mono text-xs text-ink-muted">
                  via TechCrunch
                </span>
              )
            ) : (
              <span className="mt-2 inline-block font-mono text-xs text-ink-muted">
                potential competitor (AI-inferred)
              </span>
            )}

            {c.description && (
              <p className="mt-2 text-sm text-ink-soft leading-snug">
                {c.description}
              </p>
            )}

            {c.reasoning && (
              <p className="mt-2 text-xs text-ink-muted leading-snug">
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
