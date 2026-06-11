// Server component — renders the Investors section on /c/[slug].
// No "use client": read-only display, all data flows in via props.
//
// Combines two sources into one de-duplicated list (case-insensitive by name):
//   1. company-level investors from `company_investors` (VC firms, may carry a
//      website), and
//   2. every lead/other investor named on each funding round.
// An investor is marked "lead" if it leads any round OR is flagged lead at the
// company level. A website is shown when the company-level row supplies one.

import type {
  CompanyInvestorRow,
  FundingRoundWithInvestors,
} from "@/lib/types";

interface Props {
  investors: CompanyInvestorRow[];
  rounds: FundingRoundWithInvestors[];
}

interface MergedInvestor {
  name: string; // display casing (first occurrence wins)
  website: string | null;
  isLead: boolean;
}

/**
 * Merge company-level investors and per-round investor names into one list,
 * de-duplicated case-insensitively on name. The first occurrence sets the
 * display casing; lead status and website accumulate across all occurrences.
 */
function mergeInvestors(
  investors: CompanyInvestorRow[],
  rounds: FundingRoundWithInvestors[],
): MergedInvestor[] {
  const byKey = new Map<string, MergedInvestor>();

  const upsert = (name: string, website: string | null, isLead: boolean) => {
    const trimmed = name.trim();
    if (!trimmed) return;
    const key = trimmed.toLowerCase();
    const existing = byKey.get(key);
    if (existing) {
      existing.isLead = existing.isLead || isLead;
      // Keep the first non-null website we encounter.
      if (!existing.website && website) existing.website = website;
    } else {
      byKey.set(key, { name: trimmed, website, isLead });
    }
  };

  // Company-level investors first so their website + casing take precedence.
  for (const inv of investors) {
    upsert(inv.name, inv.website, inv.isLead);
  }
  for (const round of rounds) {
    for (const name of round.leadInvestors) upsert(name, null, true);
    for (const name of round.otherInvestors) upsert(name, null, false);
  }

  // Leads first, then alphabetical (case-insensitive) within each group.
  return Array.from(byKey.values()).sort((a, b) => {
    if (a.isLead !== b.isLead) return a.isLead ? -1 : 1;
    return a.name.localeCompare(b.name, "en-US", { sensitivity: "base" });
  });
}

export function Investors({ investors, rounds }: Props) {
  const merged = mergeInvestors(investors, rounds);

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Investors</h2>

      {merged.length === 0 ? (
        <p className="text-sm text-ink-muted">No investors recorded yet.</p>
      ) : (
        <ul className="flex flex-wrap gap-2">
          {merged.map((inv) => (
            <li
              key={inv.name.toLowerCase()}
              className="inline-flex items-center gap-1.5 rounded-full border border-edge px-3 py-1 text-sm text-ink-soft"
            >
              {inv.website ? (
                <a
                  href={inv.website}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:text-ink underline underline-offset-2 decoration-ink-faint"
                >
                  {inv.name}
                </a>
              ) : (
                <span>{inv.name}</span>
              )}
              {inv.isLead && (
                <span className="text-xs uppercase tracking-wider text-ink-muted">
                  lead
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
