// Server component — renders the "Related companies" section on /c/[slug], the
// first user-visible payoff of the startup relationship graph. No "use client":
// read-only display, all data flows in via props.
//
// Two subgroups:
//   1. Similar companies — two data sources, better one wins per company:
//      a. Embedding neighbors (`similarByDescription`): nearest neighbors by
//         cosine similarity over the pipeline-computed description embeddings
//         (companies.embedding, migration 0033). When present these REPLACE
//         the heuristic edges — they rank by what the companies actually do,
//         not just shared labels. Each card carries a muted similarity caption
//         as its provenance (the derived fact's visible source).
//      b. Heuristic fallback (`similar`): directed `company_relationships`
//         edges of type 'similar' (shared industry + tag overlap), each
//         carrying its `evidence` caption. Shown only while the company has
//         no embedding yet — the section degrades, never fabricates.
//   2. Also backed by — companies sharing one or more (low-degree) investors,
//      computed read-time, captioned with the shared investor names.
// Cards mirror the Competitors section: hairline border, internal /c/[slug]
// link with the company name in ink, a clamped description, and a muted subline.
//
// Excluded companies never appear in any list: the similar_companies() SQL
// function filters them server-side and both query helpers drop them again
// defensively (the null-out convention — an excluded slug 404s).

import Link from "next/link";
import type {
  AlsoBackedByCompany,
  RelatedCompany,
  SimilarCompany,
} from "@/lib/types";

interface Props {
  similar: RelatedCompany[];
  similarByDescription: SimilarCompany[];
  alsoBackedBy: AlsoBackedByCompany[];
}

/**
 * Cosine similarity → the per-card provenance caption, e.g.
 * "93% description similarity". Clamped to [0, 99]: a negative cosine can't
 * render as a negative percentage, and 100% would overclaim on float rounding.
 */
function similarityCaption(similarity: number): string {
  const pct = Math.min(99, Math.max(0, Math.round(similarity * 100)));
  return `${pct}% description similarity`;
}

export function RelatedCompanies({
  similar,
  similarByDescription,
  alsoBackedBy,
}: Props) {
  if (
    similar.length === 0 &&
    similarByDescription.length === 0 &&
    alsoBackedBy.length === 0
  ) {
    // Section omitted entirely when there's nothing to show — sparse graph
    // data is normal early on. Same convention as Competitors (spec §11:
    // unknown = hidden).
    return null;
  }

  const useEmbeddings = similarByDescription.length > 0;

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Related companies</h2>

      {useEmbeddings ? (
        <div className="mb-8">
          <h3 className="text-xs font-medium uppercase tracking-wider text-ink-muted mb-3">
            Similar companies
          </h3>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {similarByDescription.map((c) => (
              <article
                key={c.slug}
                className="rounded-lg border border-edge p-4"
              >
                <header className="flex items-baseline gap-2">
                  <Link
                    href={`/c/${c.slug}`}
                    className="font-semibold text-ink hover:underline underline-offset-2"
                  >
                    {c.name}
                  </Link>
                  {c.industryGroup && (
                    <span className="ml-auto truncate text-xs text-ink-muted">
                      {c.industryGroup}
                    </span>
                  )}
                </header>

                {c.descriptionShort && (
                  <p className="mt-2 text-sm text-ink-soft leading-snug line-clamp-2">
                    {c.descriptionShort}
                  </p>
                )}

                {/* Provenance, comment-style: this list is computed from the
                    two companies' descriptions, and says so. */}
                <p className="mt-2 font-mono text-xs text-ink-muted leading-snug">
                  {similarityCaption(c.similarity)}
                </p>
              </article>
            ))}
          </div>
        </div>
      ) : (
        similar.length > 0 && (
          <div className="mb-8">
            <h3 className="text-xs font-medium uppercase tracking-wider text-ink-muted mb-3">
              Similar companies
            </h3>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {similar.map((c) => (
                <article
                  key={c.slug}
                  className="rounded-lg border border-edge p-4"
                >
                  <header className="flex items-baseline gap-2">
                    <Link
                      href={`/c/${c.slug}`}
                      className="font-semibold text-ink hover:underline underline-offset-2"
                    >
                      {c.name}
                    </Link>
                    {c.industryGroup && (
                      <span className="ml-auto truncate text-xs text-ink-muted">
                        {c.industryGroup}
                      </span>
                    )}
                  </header>

                  {c.descriptionShort && (
                    <p className="mt-2 text-sm text-ink-soft leading-snug line-clamp-2">
                      {c.descriptionShort}
                    </p>
                  )}

                  {/* Provenance, comment-style: where this "similar" edge came
                      from (every rendered relationship needs a visible source). */}
                  {c.evidence && (
                    <p className="mt-2 font-mono text-xs text-ink-muted leading-snug">
                      {c.evidence}
                    </p>
                  )}
                </article>
              ))}
            </div>
          </div>
        )
      )}

      {alsoBackedBy.length > 0 && (
        <div>
          <h3 className="text-xs font-medium uppercase tracking-wider text-ink-muted mb-3">
            Also backed by their investors
          </h3>
          <ul className="divide-y divide-edge">
            {alsoBackedBy.map((c) => (
              <li key={c.slug} className="py-3">
                <Link
                  href={`/c/${c.slug}`}
                  className="font-medium text-ink hover:underline underline-offset-2"
                >
                  {c.name}
                </Link>
                {c.sharedInvestors.length > 0 && (
                  <p className="mt-1 font-mono text-xs text-ink-muted">
                    Also backed by {c.sharedInvestors.join(", ")}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
