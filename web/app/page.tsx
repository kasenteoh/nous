// Front page — spotlight deck + margin notes (spec §2). No searchParams, so
// the route stays static + ISR; old /?q=… URLs simply render this page.
export const revalidate = 21600;

import Link from "next/link";
import { SpotlightDeck } from "@/components/SpotlightDeck";
import { buildSpotlightPool } from "@/lib/spotlight";
import {
  countCompanies,
  getIndustrySummary,
  listNewestCompanies,
  listRecentFundings,
} from "@/lib/queries";
import { formatDate, formatUsd } from "@/lib/format";

const labelClass =
  "text-[11px] font-medium uppercase tracking-[0.14em] text-ink-muted";

export default async function FrontPage() {
  const [spotlights, fundings, newest, industries, total] = await Promise.all([
    buildSpotlightPool(),
    listRecentFundings(5),
    listNewestCompanies(4),
    getIndustrySummary(6),
    countCompanies(),
  ]);

  const hasMarginNotes = fundings.length > 0 || newest.length > 0;

  return (
    <main className="flex-1 w-full max-w-6xl mx-auto px-6 flex flex-col">
      <div className="flex-1 grid gap-12 md:grid-cols-3 py-14 md:py-20">
        {/* ── Spotlight deck (~⅔) ─────────────────────────────────────── */}
        <div className="md:col-span-2">
          {spotlights.length > 0 ? (
            <SpotlightDeck spotlights={spotlights} />
          ) : (
            <section>
              <p className={labelClass}>Today&rsquo;s spotlight</p>
              <h1 className="mt-5 text-4xl font-bold tracking-tight text-ink">
                Nothing to spotlight yet
              </h1>
              <p className="mt-4 text-lg text-ink-muted leading-relaxed max-w-lg">
                The index is still filling. Browse what&rsquo;s here already,
                or check back soon.
              </p>
              <p className="mt-7">
                <Link
                  href="/companies"
                  className="text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
                >
                  Browse companies
                </Link>
              </p>
            </section>
          )}
        </div>

        {/* ── Margin notes (~⅓, hairline left border) ─────────────────── */}
        {hasMarginNotes && (
          <aside className="md:border-l md:border-edge md:pl-10 space-y-10 min-w-0">
            {fundings.length > 0 && (
              <section aria-label="Recent fundings">
                <h2 className={labelClass}>Recent fundings</h2>
                <ul className="mt-3 space-y-2.5">
                  {fundings.map((funding, i) => (
                    <li
                      key={`${funding.companySlug}-${funding.announced_date}-${i}`}
                      className="text-sm leading-snug truncate"
                    >
                      <Link
                        href={`/c/${funding.companySlug}`}
                        className="font-semibold text-ink hover:underline underline-offset-2"
                      >
                        {funding.companyName}
                      </Link>{" "}
                      {/* Skip the green span entirely when the amount is
                          unknown — never a green "—" (spec §2). */}
                      {funding.amount_raised != null &&
                        funding.amount_raised > 0 && (
                          <span className="font-mono text-money">
                            {formatUsd(funding.amount_raised)}
                          </span>
                        )}{" "}
                      <span className="font-mono text-xs text-ink-muted">
                        {funding.round_type ? `${funding.round_type} · ` : ""}
                        {formatDate(funding.announced_date)}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {newest.length > 0 && (
              <section aria-label="New on nous">
                <h2 className={labelClass}>New on nous</h2>
                <ul className="mt-3 space-y-2.5">
                  {newest.map((company) => (
                    <li
                      key={company.slug}
                      className="text-sm leading-snug truncate"
                    >
                      <Link
                        href={`/c/${company.slug}`}
                        className="font-semibold text-ink hover:underline underline-offset-2"
                      >
                        {company.name}
                      </Link>
                      {company.description_short && (
                        <span className="text-ink-muted">
                          {" "}
                          · {company.description_short}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </aside>
        )}
      </div>

      {/* ── Bottom hairline row: top industries + browse-all ──────────── */}
      {total > 0 && (
        <div className="border-t border-edge py-5 mb-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
          {industries.top.map((group) => (
            <Link
              key={group}
              href={`/companies?industry=${encodeURIComponent(group)}`}
              className="text-ink-soft hover:text-ink transition-colors"
            >
              {group}
            </Link>
          ))}
          {industries.moreCount > 0 && (
            <Link
              href="/companies"
              className="text-ink-faint hover:text-ink-muted transition-colors"
            >
              +{industries.moreCount} more
            </Link>
          )}
          <Link
            href="/companies"
            className="ml-auto whitespace-nowrap text-ink underline underline-offset-4 decoration-ink/30 hover:decoration-ink transition-colors"
          >
            Browse all {total.toLocaleString("en-US")} →
          </Link>
        </div>
      )}
    </main>
  );
}
