import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "About — nous",
  description:
    "How nous discovers US software startups, where its data comes from, and how its company pages are built.",
};

export default function AboutPage() {
  return (
    <main className="flex-1 px-6 py-12 max-w-3xl mx-auto w-full">
      <header className="mb-10">
        <h1 className="text-4xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          About nous
        </h1>
        <p className="mt-3 text-lg text-zinc-500 dark:text-zinc-400 leading-relaxed">
          nous is an automated directory of US software startups, assembled from
          public sources and refreshed on a schedule. Every figure on a company
          page traces back to a recorded source.
        </p>
      </header>

      <section className="mb-10">
        <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-3">
          How companies are discovered
        </h2>
        <p className="text-sm text-zinc-600 dark:text-zinc-400 leading-relaxed">
          Companies enter the index two ways, and each page records which one
          surfaced it (the “Discovered via” badge):
        </p>
        <ul className="mt-3 space-y-2 text-sm text-zinc-600 dark:text-zinc-400 leading-relaxed list-disc pl-5">
          <li>
            <span className="font-medium text-zinc-800 dark:text-zinc-200">
              VC portfolios
            </span>{" "}
            — the public portfolio pages of leading venture firms (Y Combinator,
            Andreessen Horowitz, Sequoia, Lightspeed, Founders Fund, Greylock,
            Khosla, and others). The firm that surfaced a company is recorded as
            an investor.
          </li>
          <li>
            <span className="font-medium text-zinc-800 dark:text-zinc-200">
              Funding news
            </span>{" "}
            — funding announcements from Google News and TechCrunch’s venture
            coverage.
          </li>
        </ul>
      </section>

      <section className="mb-10">
        <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-3">
          How company pages are built
        </h2>
        <p className="text-sm text-zinc-600 dark:text-zinc-400 leading-relaxed">
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
        <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-3">
          Sourcing &amp; attribution
        </h2>
        <p className="text-sm text-zinc-600 dark:text-zinc-400 leading-relaxed">
          Every fact rendered on a company page is tied to a source: funding
          figures link to the news article they came from, descriptions and
          people derive from the company’s own site, and employee estimates name
          the source they were drawn from. Figures extracted with low confidence
          are flagged as such. Scraping follows each site’s robots policy and is
          rate-limited out of courtesy.
        </p>
      </section>

      <section className="mb-10">
        <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-3">
          How often it updates
        </h2>
        <ul className="space-y-2 text-sm text-zinc-600 dark:text-zinc-400 leading-relaxed list-disc pl-5">
          <li>
            <span className="font-medium text-zinc-800 dark:text-zinc-200">
              Weekly
            </span>{" "}
            — discovery of new companies from VC portfolios, de-duplication, and
            competitor analysis.
          </li>
          <li>
            <span className="font-medium text-zinc-800 dark:text-zinc-200">
              Weekly
            </span>{" "}
            — website reading and the descriptions / leadership it produces.
          </li>
          <li>
            <span className="font-medium text-zinc-800 dark:text-zinc-200">
              Daily
            </span>{" "}
            — funding news ingestion and round extraction, so new raises appear
            quickly.
          </li>
        </ul>
        <p className="mt-3 text-sm text-zinc-500 dark:text-zinc-500 leading-relaxed">
          Pages are cached and refresh within a few hours of the underlying data
          changing.
        </p>
      </section>

      <section className="mb-4">
        <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-3">
          Caveats
        </h2>
        <p className="text-sm text-zinc-500 dark:text-zinc-500 leading-relaxed">
          nous is automated and assembled from public, sometimes-imperfect
          sources, so individual figures may be incomplete or out of date. It is
          an informational directory, not investment advice.
        </p>
      </section>
    </main>
  );
}
