// Front page — spotlight deck + margin notes (spec §2). No searchParams, so
// the route stays static + ISR; old /?q=… URLs simply render this page.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { JsonLd } from "@/components/JsonLd";
import { SpotlightDeck } from "@/components/SpotlightDeck";
import { buildSpotlightPool } from "@/lib/spotlight";
import {
  countCompanies,
  countNewThisWeek,
  getIndustrySummary,
  listHeatingUpCompanies,
  listNewestCompanies,
  listRecentFundings,
  type TrendingCompany,
} from "@/lib/queries";
import { formatDate, formatUsd, formatUsdExact } from "@/lib/format";
import { SITE_NAME, siteOrigin } from "@/lib/site";

// Title and description inherit the layout defaults; only the canonical is
// page-specific (resolved against metadataBase). NOTE: a page-level
// `alternates` replaces the layout's object wholesale (Next's metadata merge
// is shallow per key), so the RSS autodiscovery link must be restated here —
// the bare `{ canonical }` form silently dropped it on the homepage only
// (2026-07 QA finding).
export const metadata: Metadata = {
  alternates: {
    canonical: "/",
    types: {
      "application/rss+xml": [
        { url: "/feed.xml", title: "nous — new funding & news" },
      ],
    },
  },
};

const labelClass =
  "text-[11px] font-medium uppercase tracking-[0.14em] text-ink-muted";

export default async function FrontPage() {
  const [spotlights, fundings, newest, industries, total, newCounts] =
    await Promise.all([
      buildSpotlightPool(),
      listRecentFundings(5),
      listNewestCompanies(4),
      getIndustrySummary(6),
      countCompanies(),
      countNewThisWeek(),
    ]);
  const heating = await listHeatingUpCompanies(6);

  const hasMarginNotes = fundings.length > 0 || newest.length > 0;

  // Company strip below the deck. Preferred source: the top momentum-scored
  // companies — the SAME signal /trending ranks by, labeled "Heating up" to
  // match it, so the homepage can never claim trending picks while /trending
  // reports no scores (2026-07 QA finding: the old spotlight-derived
  // "Trending now" strip contradicted the empty momentum page). Until scores
  // exist on prod, fall back to the spotlight pool under a neutral label that
  // makes no momentum claim. The momentum "why" chips ride as the fact line.
  const momentumStrip: TrendingCompany[] = heating.map((c) => ({
    slug: c.slug,
    name: c.name,
    oneLiner: c.description_short ?? "",
    facts: c.momentumWhy.slice(0, 2),
  }));
  const stripIsMomentum = momentumStrip.length > 1;
  const trending: TrendingCompany[] = stripIsMomentum
    ? momentumStrip
    : spotlights.slice(1, 7).map((s) => ({
        slug: s.slug,
        name: s.name,
        oneLiner: s.oneLiner,
        facts: s.facts,
      }));
  const stripLabel = stripIsMomentum ? "Heating up" : "More to watch";

  const origin = siteOrigin();

  return (
    <main className="flex-1 w-full max-w-6xl mx-auto px-6 flex flex-col">
      {/* Stable, descriptive page heading. The visible "headline" is the
          rotating spotlight company name (an <h2> inside an aria-live region),
          which would otherwise leave the home page with no constant <h1> and
          send screen-reader heading-nav to an arbitrary company name. */}
      <h1 className="sr-only">nous — US software startup discovery</h1>
      {/* Structured data: the site operator + sitelinks-searchbox wiring into
          the /companies?q=… search the masthead form already exposes. */}
      <JsonLd
        data={{
          "@context": "https://schema.org",
          "@type": "Organization",
          name: SITE_NAME,
          url: origin,
        }}
      />
      <JsonLd
        data={{
          "@context": "https://schema.org",
          "@type": "WebSite",
          name: SITE_NAME,
          url: origin,
          potentialAction: {
            "@type": "SearchAction",
            target: `${origin}/companies?q={search_term_string}`,
            "query-input": "required name=search_term_string",
          },
        }}
      />
      <div className="flex-1 grid gap-12 md:grid-cols-3 py-14 md:py-20">
        {/* ── Spotlight deck (~⅔) ─────────────────────────────────────── */}
        <div className="md:col-span-2">
          {spotlights.length > 0 ? (
            <SpotlightDeck spotlights={spotlights} />
          ) : (
            <section>
              <p className={labelClass}>Today&rsquo;s spotlight</p>
              <h2 className="mt-5 text-4xl font-bold tracking-tight text-ink">
                Nothing to spotlight yet
              </h2>
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
            {/* "New this week" count line — hidden when both counts are 0 so
                the aside layout is unchanged during quiet weeks. */}
            {(newCounts.companies > 0 || newCounts.rounds > 0) && (
              <section aria-label="New this week">
                <p className="text-sm font-mono text-ink-muted leading-snug">
                  <Link
                    href="/new"
                    className="text-accent hover:underline underline-offset-2"
                  >
                    New this week
                  </Link>
                  {": "}
                  {newCounts.companies.toLocaleString("en-US")} companies,{" "}
                  {newCounts.rounds.toLocaleString("en-US")} rounds →
                </p>
              </section>
            )}

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
                          <span
                            className="font-mono text-money"
                            title={formatUsdExact(funding.amount_raised)}
                          >
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

      {/* ── Company strip ─────────────────────────────────────────────────
          "Heating up" (momentum-ranked, matching /trending) once scores
          exist; otherwise a neutral "More to watch" from the spotlight pool.
          Renders nothing when the source has ≤1 entry, so it never shows a
          lonely or empty row. */}
      {trending.length > 0 && (
        <section
          aria-label={stripLabel}
          className="border-t border-edge py-7"
        >
          <p className={labelClass}>
            {stripLabel}
            {stripIsMomentum && (
              <>
                {" "}
                <Link
                  href="/trending"
                  className="normal-case tracking-normal font-normal text-ink-muted hover:text-ink underline underline-offset-2 decoration-ink-faint"
                >
                  see all →
                </Link>
              </>
            )}
          </p>
          <ul className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {trending.map((company) => (
              <li key={company.slug}>
                <Link
                  href={`/c/${company.slug}`}
                  className="group block rounded-lg border border-edge p-4 hover:border-ink-muted transition-colors"
                >
                  <span className="font-semibold text-ink group-hover:underline underline-offset-2 leading-snug">
                    {company.name}
                  </span>
                  <span className="mt-1.5 block text-sm text-ink-muted line-clamp-2 leading-snug">
                    {company.oneLiner}
                  </span>
                  {company.facts.length > 0 && (
                    <span className="mt-2 block font-mono text-xs text-ink-muted">
                      {company.facts.join(" · ")}
                    </span>
                  )}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

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
              className="text-ink-muted hover:text-ink transition-colors"
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
