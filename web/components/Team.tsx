// Server component — renders the leadership/founders section on /c/[slug].
// Read-only display; data (extracted from the company website during
// enrich-companies) flows in via props. Section is omitted when empty, same
// convention as FundingHistory / Competitors (spec §11: unknown = hidden).

import type { PersonRow } from "@/lib/types";

interface Props {
  people: PersonRow[];
  companyName: string;
}

export function Team({ people, companyName }: Props) {
  if (people.length === 0) {
    return null;
  }

  // Every person carries the same website source_url; surface one attribution
  // line for the section rather than repeating it per row (spec §11).
  const sourceUrl = people.find((p) => p.source_url)?.source_url ?? null;

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-4">
        Leadership
      </h2>

      <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
        {people.map((person) => (
          <li
            key={person.id}
            className="py-3 flex flex-col sm:flex-row sm:items-baseline gap-1 sm:gap-4"
          >
            <span className="font-medium text-zinc-900 dark:text-zinc-100">
              {person.name}
            </span>
            <span className="text-sm text-zinc-500 dark:text-zinc-400">
              {person.role}
            </span>
          </li>
        ))}
      </ul>

      {sourceUrl && (
        <p className="mt-3 text-xs text-zinc-400 dark:text-zinc-500">
          From{" "}
          <a
            href={sourceUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="underline underline-offset-2 hover:text-zinc-600 dark:hover:text-zinc-300"
          >
            {companyName}&rsquo;s website
          </a>
          .
        </p>
      )}
    </section>
  );
}
