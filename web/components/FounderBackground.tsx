// Server component — renders the "Founder background" section on /c/[slug]:
// where the company's founders/execs worked BEFORE this company, from the
// career_moves table (extract-career-history / the talent-flow rider).
//
// Read-only display; data flows in via props. Section is omitted when empty,
// same convention as Team / Competitors (spec §11: unknown = hidden). Source
// attribution (the founders' website) lives in the consolidated Sources section
// at the bottom of the page, so no inline "from <company>'s website" link here.
//
// The #184 probe found named pedigrees are thin (~1 in 6 companies), so most
// pages render nothing — by design, not a gap.

import Link from "next/link";

import type { CareerMove } from "@/lib/types";

interface Props {
  careerMoves: CareerMove[];
}

function tenure(startYear: number | null, endYear: number | null): string | null {
  // Every row is a PRIOR employer (the founder has left it), so a missing end
  // year means UNKNOWN — never "present" (that would fabricate an unsourced
  // current-employment claim). Unknown stays unknown: show only what's stated.
  if (startYear && endYear) return `${startYear}–${endYear}`;
  if (startYear) return `from ${startYear}`;
  if (endYear) return `until ${endYear}`;
  return null;
}

export function FounderBackground({ careerMoves }: Props) {
  if (careerMoves.length === 0) {
    return null;
  }

  // Group by person, preserving first-seen order (the query orders by
  // person_normalized_name, so a person's rows are already contiguous).
  const byPerson = new Map<string, CareerMove[]>();
  for (const move of careerMoves) {
    const existing = byPerson.get(move.personName);
    if (existing) {
      existing.push(move);
    } else {
      byPerson.set(move.personName, [move]);
    }
  }

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-1">Founder background</h2>
      <p className="text-sm text-ink-muted mb-4">
        Where the team worked before, from the company&apos;s website.
      </p>

      <ul className="divide-y divide-edge">
        {Array.from(byPerson.entries()).map(([personName, moves]) => (
          <li key={personName} className="py-3">
            <span className="font-medium text-ink">{personName}</span>
            <ul className="mt-1 flex flex-col gap-1">
              {moves.map((move, i) => {
                const span = tenure(move.startYear, move.endYear);
                return (
                  <li key={i} className="text-sm text-ink-muted">
                    {move.priorRole ? `${move.priorRole} · ` : ""}
                    {move.priorCompanySlug ? (
                      <Link
                        href={`/c/${move.priorCompanySlug}`}
                        className="text-ink hover:underline underline-offset-2"
                      >
                        {move.priorCompanyName}
                      </Link>
                    ) : (
                      <span className="text-ink">{move.priorCompanyName}</span>
                    )}
                    {span ? <span className="text-ink-muted"> ({span})</span> : null}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  );
}
