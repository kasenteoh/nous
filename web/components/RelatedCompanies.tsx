// Server component — renders the "Related companies" section on /c/[slug], the
// first user-visible payoff of the startup relationship graph. No "use client":
// read-only display, all data flows in via props.
//
// Two subgroups:
//   1. Similar companies — directed `company_relationships` edges of type
//      'similar', each carrying a muted `evidence` caption (the source/
//      attribution — every fact on a company page has a visible source).
//   2. Also backed by — companies sharing one or more (low-degree) investors,
//      computed read-time, captioned with the shared investor names.
// Cards mirror the Competitors section: hairline border, internal /c/[slug]
// link with the company name in ink, a clamped description, and a muted subline.

import Link from "next/link";
import type { AlsoBackedByCompany, RelatedCompany } from "@/lib/types";

interface Props {
  similar: RelatedCompany[];
  alsoBackedBy: AlsoBackedByCompany[];
}

export function RelatedCompanies({ similar, alsoBackedBy }: Props) {
  if (similar.length === 0 && alsoBackedBy.length === 0) {
    // Section omitted entirely when there's nothing to show — sparse graph
    // data is normal early on. Same convention as Competitors (spec §11:
    // unknown = hidden).
    return null;
  }

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Related companies</h2>

      {similar.length > 0 && (
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
                    <span className="ml-auto truncate text-xs text-ink-faint">
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
