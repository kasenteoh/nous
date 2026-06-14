// Server component — renders the leadership/founders section on /c/[slug].
// Read-only display; data (extracted from the company website during
// enrich-companies) flows in via props. Section is omitted when empty, same
// convention as FundingHistory / Competitors (spec §11: unknown = hidden).
//
// Source attribution lives in the consolidated Sources section at the bottom of
// the page (the founders' website source_url is cited there), so this section
// shows no inline "from <company>'s website" link.

import type { PersonRow } from "@/lib/types";

interface Props {
  people: PersonRow[];
}

export function Team({ people }: Props) {
  if (people.length === 0) {
    return null;
  }

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Leadership</h2>

      <ul className="divide-y divide-edge">
        {people.map((person) => (
          <li
            key={person.id}
            className="py-3 flex flex-col sm:flex-row sm:items-baseline gap-1 sm:gap-4"
          >
            <span className="font-medium text-ink">{person.name}</span>
            <span className="text-sm text-ink-muted">{person.role}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
