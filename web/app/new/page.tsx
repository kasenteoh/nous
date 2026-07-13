// /new — "New this week" feed. Shows companies and funding rounds extracted
// in the last 7 days, bucketed by UTC calendar date of created_at, newest
// date first. Revalidates every 6 hours (same cadence as the front page).
//
// Metadata: plain string + manual " — nous" suffix.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import {
  listNewThisWeekCompanies,
  listNewThisWeekFundingRounds,
  countNewThisWeek,
} from "@/lib/queries";
import { formatDate, formatUsd, formatUsdExact } from "@/lib/format";
import type {
  NewThisWeekCompanyRow,
  NewThisWeekFundingRow,
} from "@/lib/queries";

export const metadata: Metadata = {
  // Bare title — the root layout's template appends " — nous" exactly once.
  // (Previously hardcoded the suffix here, producing "… — nous — nous".)
  title: "New this week",
  description:
    "Companies discovered and funding rounds extracted in the last 7 days on nous.",
  // Self-canonical so ?param/ trailing-slash variants don't fragment indexing
  // (every sibling page already declares one).
  alternates: { canonical: "/new" },
};

// ── Label style (matches front page margin-notes idiom) ───────────────────────
const labelClass =
  "text-[11px] font-medium uppercase tracking-[0.14em] text-ink-muted";

// ── Bucket items by UTC calendar date (YYYY-MM-DD) ────────────────────────────
// Both company rows and funding rows have a `created_at` ISO timestamp; we
// strip to the date portion (UTC) so items ingested the same calendar day group
// together regardless of the time-of-day they were written.
function toUtcDate(iso: string): string {
  // Slice the first 10 chars of the ISO timestamp = "YYYY-MM-DD".
  // This is safe for both full timestamps ("2026-06-11T…") and date-only
  // strings ("2026-06-11"), and never shifts the day across timezone offsets
  // because we are treating the string as UTC throughout.
  return iso.slice(0, 10);
}

interface DayBucket {
  date: string; // "YYYY-MM-DD"
  companies: NewThisWeekCompanyRow[];
  rounds: NewThisWeekFundingRow[];
}

function bucketByDate(
  companies: NewThisWeekCompanyRow[],
  rounds: NewThisWeekFundingRow[],
): DayBucket[] {
  const map = new Map<string, DayBucket>();

  const getOrCreate = (date: string): DayBucket => {
    if (!map.has(date)) {
      map.set(date, { date, companies: [], rounds: [] });
    }
    // Non-null assertion is safe: we just ensured the key exists above.
    return map.get(date)!;
  };

  for (const c of companies) {
    getOrCreate(toUtcDate(c.created_at)).companies.push(c);
  }
  for (const r of rounds) {
    getOrCreate(toUtcDate(r.created_at)).rounds.push(r);
  }

  // Sort buckets newest date first.
  return [...map.values()].sort((a, b) => b.date.localeCompare(a.date));
}

export default async function NewThisWeekPage() {
  const [companies, rounds, counts] = await Promise.all([
    listNewThisWeekCompanies(7, 200),
    listNewThisWeekFundingRounds(7, 200),
    countNewThisWeek(),
  ]);

  const isEmpty = companies.length === 0 && rounds.length === 0;
  const buckets = bucketByDate(companies, rounds);

  return (
    <main className="flex-1 w-full max-w-3xl mx-auto px-6 py-14 md:py-20">
      {/* ── Page heading ──────────────────────────────────────────────── */}
      <header className="mb-10">
        <p className={labelClass}>discovery feed</p>
        <h1 className="mt-3 text-3xl font-bold tracking-tight text-ink">
          New this week
        </h1>
        {!isEmpty && (
          <p className="mt-2 text-sm text-ink-muted font-mono">
            {/* Lists are capped at 200 rows; the count query is uncapped and
                can exceed the visible feed. Use the larger of the two so we
                never show "0 companies" above a populated list on partial error. */}
            {Math.max(counts.companies, companies.length).toLocaleString("en-US")} companies discovered
            {" · "}
            {Math.max(counts.rounds, rounds.length).toLocaleString("en-US")} rounds extracted in the
            last 7 days
          </p>
        )}
      </header>

      {/* ── Empty state ────────────────────────────────────────────────── */}
      {isEmpty && (
        <div className="border border-edge rounded-md px-6 py-10 text-center">
          <p className="text-ink-muted">
            A quiet week — nothing new in the last 7 days.
          </p>
          <p className="mt-4 text-sm">
            <Link
              href="/companies"
              className="text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
            >
              Browse all companies →
            </Link>
          </p>
        </div>
      )}

      {/* ── Day buckets ────────────────────────────────────────────────── */}
      {buckets.length > 0 && (
        <div className="space-y-10">
          {buckets.map((bucket) => (
            <section key={bucket.date} aria-label={formatDate(bucket.date)}>
              {/* Day heading */}
              <h2 className="text-sm font-semibold text-ink border-b border-edge pb-2 mb-4">
                {formatDate(bucket.date)}
              </h2>

              {/* Companies sub-section */}
              {bucket.companies.length > 0 && (
                <div className="mb-5">
                  <p className={`${labelClass} mb-2`}>Companies</p>
                  <ul className="space-y-2">
                    {bucket.companies.map((company) => (
                      <li
                        key={company.slug}
                        className="text-sm leading-snug"
                      >
                        <Link
                          href={`/c/${company.slug}`}
                          className="font-semibold text-accent hover:underline underline-offset-2"
                        >
                          {company.name}
                        </Link>
                        {company.industry_group && (
                          <span className="ml-2 text-[11px] text-ink-muted font-mono uppercase tracking-wide">
                            {company.industry_group}
                          </span>
                        )}
                        {company.description_short && (
                          <span className="ml-1 text-ink-muted block truncate">
                            {company.description_short}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Rounds sub-section */}
              {bucket.rounds.length > 0 && (
                <div>
                  <p className={`${labelClass} mb-2`}>Rounds</p>
                  <ul className="space-y-2">
                    {bucket.rounds.map((round, i) => (
                      <li
                        key={`${round.companySlug}-${round.created_at}-${i}`}
                        className="text-sm leading-snug"
                      >
                        <Link
                          href={`/c/${round.companySlug}`}
                          className="font-semibold text-accent hover:underline underline-offset-2"
                        >
                          {round.companyName}
                        </Link>
                        {/* Only render the amount span when it is a real positive
                            number — never a green "—" (mirrors front-page spec). */}
                        {round.amount_raised != null &&
                          round.amount_raised > 0 && (
                            <span
                              className="ml-2 font-mono text-money"
                              title={formatUsdExact(round.amount_raised)}
                            >
                              {formatUsd(round.amount_raised)}
                            </span>
                          )}
                        <span className="ml-2 font-mono text-xs text-ink-muted">
                          {[
                            round.round_type,
                            round.announced_date && formatDate(round.announced_date),
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>
          ))}
        </div>
      )}
    </main>
  );
}
