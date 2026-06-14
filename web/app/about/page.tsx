import type { Metadata } from "next";
import { getCoverageStats } from "@/lib/queries";
import { formatDate } from "@/lib/format";

// Static page + ISR: the coverage line is computed from the catalog but rarely
// changes within a window, so cache it on the same 6h cadence as the rest of
// the site rather than rendering dynamically on every request.
export const revalidate = 21600;

export const metadata: Metadata = {
  // The layout's title template appends " — nous".
  title: "About",
  description:
    "How nous discovers US software startups, where its data comes from, and how its company pages are built.",
  alternates: { canonical: "/about" },
};

export default async function AboutPage() {
  const coverage = await getCoverageStats();

  return (
    <main className="flex-1 px-6 py-12 max-w-3xl mx-auto w-full">
      <header className="mb-10">
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          About nous
        </h1>
        <p className="mt-3 text-lg text-ink-soft leading-relaxed">
          nous is an automated directory of US software startups, assembled from
          public sources and refreshed on a schedule. Every figure on a company
          page traces back to a recorded source.
        </p>
        {/* Coverage / freshness honesty line — suppressed entirely when the
            catalog count is unavailable (missing env / query error) so we never
            print a misleading "0 of 0". */}
        {coverage.shown > 0 && (
          <p className="mt-4 font-mono text-sm text-ink-muted leading-relaxed">
            {coverage.withFunding.toLocaleString("en-US")} of{" "}
            {coverage.shown.toLocaleString("en-US")} listed companies have
            funding data{" "}
            <span className="text-ink-faint">
              · catalog refreshed every 6 hours
              {coverage.asOf && <> · last updated {formatDate(coverage.asOf)}</>}
            </span>
          </p>
        )}
      </header>

      <section className="mb-10">
        <h2 className="text-lg font-semibold text-ink mb-3">
          How companies are discovered
        </h2>
        <p className="text-sm text-ink-soft leading-relaxed">
          Companies enter the index two ways, and each page records which one
          surfaced it (the “Discovered via” badge):
        </p>
        <ul className="mt-3 space-y-2 text-sm text-ink-soft leading-relaxed list-disc pl-5">
          <li>
            <span className="font-medium text-ink">
              VC portfolios
            </span>{" "}
            — the public portfolio pages of leading venture firms (Y Combinator,
            Andreessen Horowitz, Sequoia, Lightspeed, Founders Fund, Greylock,
            Khosla, and others). The firm that surfaced a company is recorded as
            an investor.
          </li>
          <li>
            <span className="font-medium text-ink">
              Funding news
            </span>{" "}
            — funding announcements from Google News and TechCrunch’s venture
            coverage.
          </li>
        </ul>
      </section>

      <section className="mb-10">
        <h2 className="text-lg font-semibold text-ink mb-3">
          How company pages are built
        </h2>
        <p className="text-sm text-ink-soft leading-relaxed">
          Once a company is known, nous resolves and reads its public website,
          then uses a large language model to draft the description, category,
          and leadership details from that page. Funding rounds, amounts, and
          investors are extracted from the news coverage above; competitors are
          inferred from a company’s public profile and coverage. Employee-count
          ranges are estimated from public sources where available. The model is
          instructed to leave a field blank rather than guess — unknown values
          stay unknown.
        </p>
      </section>

      <section className="mb-10">
        <h2 className="text-lg font-semibold text-ink mb-3">
          Sourcing &amp; attribution
        </h2>
        <p className="text-sm text-ink-soft leading-relaxed">
          Every fact rendered on a company page is tied to a source: funding
          figures link to the news article they came from, descriptions and
          people derive from the company’s own site, and employee estimates name
          the source they were drawn from. Figures extracted with low confidence
          are flagged as such. Scraping follows each site’s robots policy and is
          rate-limited out of courtesy.
        </p>
      </section>

      <section className="mb-10">
        <h2 className="text-lg font-semibold text-ink mb-3">
          How often it updates
        </h2>
        <ul className="space-y-2 text-sm text-ink-soft leading-relaxed list-disc pl-5">
          <li>
            <span className="font-medium text-ink">
              Weekly
            </span>{" "}
            — discovery of new companies from VC portfolios, de-duplication, and
            competitor analysis.
          </li>
          <li>
            <span className="font-medium text-ink">
              Weekly
            </span>{" "}
            — website reading and the descriptions / leadership it produces.
          </li>
          <li>
            <span className="font-medium text-ink">
              Daily
            </span>{" "}
            — funding news ingestion and round extraction, so new raises appear
            quickly.
          </li>
        </ul>
        <p className="mt-3 text-sm text-ink-muted leading-relaxed">
          Pages are cached and refresh within a few hours of the underlying data
          changing.
        </p>
      </section>

      <section className="mb-4">
        <h2 className="text-lg font-semibold text-ink mb-3">
          Caveats
        </h2>
        <p className="text-sm text-ink-muted leading-relaxed">
          nous is automated and assembled from public, sometimes-imperfect
          sources, so individual figures may be incomplete or out of date. It is
          an informational directory, not investment advice.
        </p>
      </section>
    </main>
  );
}
