// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { getInvestorBySlug } from "@/lib/queries";
import { formatDate, formatUsd } from "@/lib/format";
import { CompanyCard } from "@/components/CompanyCard";

type Props = {
  params: Promise<{ slug: string }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const investor = await getInvestorBySlug(slug);

  if (!investor) {
    // The layout's title template appends " — nous".
    return { title: "Investor not found" };
  }

  // Use portfolioCount (the denormalized total from migration 0025) for the
  // SEO description so it matches the /investors index count.
  const count = investor.portfolioCount;
  const description =
    investor.description ??
    (count > 0
      ? `${investor.name} backs ${count} ${count === 1 ? "company" : "companies"} indexed on nous, with funding history and recent activity.`
      : `${investor.name} — investor profile, portfolio, and funding activity on nous.`);

  return {
    // Bare investor name — the layout's title template appends " — nous".
    title: investor.name,
    description,
    alternates: { canonical: `/investor/${slug}` },
  };
}

/** Render-friendly hostname for a URL — strips protocol, "www.", trailing slash. */
function websiteHostname(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const host = new URL(url).host.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return null;
  }
}

export default async function InvestorPage({ params }: Props) {
  const { slug } = await params;
  const investor = await getInvestorBySlug(slug);

  if (!investor) {
    notFound();
  }

  const { name, type, description, website, portfolio, portfolioCount, rounds } = investor;

  // Recent activity = the most recent rounds with a known date (the rounds list
  // is already sorted newest-first, nulls last).
  const recentRounds = rounds.filter((r) => r.announced_date !== null).slice(0, 8);
  const leadCount = rounds.filter((r) => r.isLead).length;

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="mb-10">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-4xl font-semibold tracking-tight text-ink">
            {name}
          </h1>
          {type !== "unknown" && (
            <span
              className="rounded border border-edge px-2 py-0.5 text-xs text-ink-muted"
              title="Investor type"
            >
              {type}
            </span>
          )}
        </div>

        <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-2 text-sm text-ink-muted">
          {website && (
            <div>
              <dt className="sr-only">Website</dt>
              <dd>
                <a
                  href={website}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-ink-soft hover:text-ink underline underline-offset-2 decoration-ink-faint"
                >
                  {websiteHostname(website) ?? website}
                </a>
              </dd>
            </div>
          )}
          <div>
            <dt className="sr-only">Portfolio size</dt>
            {/* portfolioCount uses the denormalized column from migration 0025:
                counts companies via EITHER company_investors OR funding rounds,
                matching the /investors index. The rendered portfolio cards below
                only show company_investors-linked companies (round-only companies
                are listed in the Funding activity table instead). */}
            <dd>
              Backs {portfolioCount.toLocaleString("en-US")}{" "}
              {portfolioCount === 1 ? "company" : "companies"}
            </dd>
          </div>
          {leadCount > 0 && (
            <div>
              <dt className="sr-only">Rounds led</dt>
              <dd>
                Led {leadCount.toLocaleString("en-US")}{" "}
                {leadCount === 1 ? "round" : "rounds"}
              </dd>
            </div>
          )}
        </dl>

        {description && (
          <p className="mt-5 text-base text-ink-soft leading-relaxed max-w-2xl">
            {description}
          </p>
        )}
      </header>

      {/* ── Portfolio ───────────────────────────────────────────────────────── */}
      <section className="mb-12">
        <h2 className="text-lg font-semibold text-ink mb-4">Portfolio</h2>
        {portfolio.length === 0 ? (
          <p className="text-sm text-ink-muted">
            No portfolio companies recorded yet.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {portfolio.map((company) => (
              <CompanyCard key={company.slug} company={company} />
            ))}
          </div>
        )}
      </section>

      {/* ── Funding activity ────────────────────────────────────────────────── */}
      <section className="mb-12">
        <h2 className="text-lg font-semibold text-ink mb-4">Funding activity</h2>
        {rounds.length === 0 ? (
          <p className="text-sm text-ink-muted">
            No funding rounds recorded for this investor yet.
          </p>
        ) : (
          <div className="overflow-x-auto -mx-6 px-6">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-edge text-left text-ink-muted">
                  <th className="py-2 pr-6 font-medium">Date</th>
                  <th className="py-2 pr-6 font-medium">Company</th>
                  <th className="py-2 pr-6 font-medium">Round</th>
                  <th className="py-2 pr-6 font-medium text-right">Amount</th>
                  <th className="py-2 font-medium">Role</th>
                </tr>
              </thead>
              <tbody>
                {rounds.map((round) => (
                  <tr
                    key={round.roundId}
                    className="border-b border-edge hover:bg-edge/30 align-top"
                  >
                    <td className="py-3 pr-6 font-mono text-ink-muted">
                      {round.announced_date
                        ? formatDate(round.announced_date)
                        : "—"}
                    </td>
                    <td className="py-3 pr-6 text-ink-soft">
                      <Link
                        href={`/c/${round.companySlug}`}
                        className="hover:text-ink underline underline-offset-2 decoration-ink-faint"
                      >
                        {round.companyName}
                      </Link>
                    </td>
                    <td className="py-3 pr-6 text-ink-soft">
                      {round.round_type ?? "—"}
                    </td>
                    <td className="py-3 pr-6 text-right font-mono">
                      {round.amount_raised != null ? (
                        <span className="text-money">
                          {formatUsd(round.amount_raised)}
                        </span>
                      ) : (
                        <span className="text-ink-faint">—</span>
                      )}
                    </td>
                    <td className="py-3 text-ink-soft">
                      {round.isLead ? (
                        <span className="text-xs uppercase tracking-wider text-ink-muted">
                          lead
                        </span>
                      ) : (
                        "participant"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ── Recent activity summary ─────────────────────────────────────────── */}
      {recentRounds.length > 0 && (
        <section className="mb-12">
          <h2 className="text-lg font-semibold text-ink mb-4">
            Recent activity
          </h2>
          <ul className="space-y-2 text-sm text-ink-soft">
            {recentRounds.map((round) => (
              <li key={round.roundId} className="flex flex-wrap gap-x-2">
                <span className="font-mono text-ink-muted">
                  {formatDate(round.announced_date)}
                </span>
                <span>
                  {round.isLead ? "Led" : "Joined"}{" "}
                  <Link
                    href={`/c/${round.companySlug}`}
                    className="hover:text-ink underline underline-offset-2 decoration-ink-faint"
                  >
                    {round.companyName}
                  </Link>
                  {round.round_type ? ` ${round.round_type}` : ""}
                  {round.amount_raised != null
                    ? ` (${formatUsd(round.amount_raised)})`
                    : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <Link
        href="/investors"
        className="text-sm font-medium text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
      >
        ← All investors
      </Link>
    </main>
  );
}
