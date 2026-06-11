// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getCompanyBySlug } from "@/lib/queries";
import { formatEmployeeRange, formatLocation, formatUsd } from "@/lib/format";
import { Markdown } from "@/components/Markdown";
import { Team } from "@/components/Team";
import { FundingHistory } from "@/components/FundingHistory";
import { Investors } from "@/components/Investors";
import { Competitors } from "@/components/Competitors";
import { News } from "@/components/News";

// ─── Types ────────────────────────────────────────────────────────────────────

type Props = {
  params: Promise<{ slug: string }>;
};

// ─── Metadata ─────────────────────────────────────────────────────────────────

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const detail = await getCompanyBySlug(slug);

  if (!detail) {
    return { title: "Company not found — nous" };
  }

  const { company } = detail;

  // Prefer the LLM-generated short description; fall back to a location/industry summary.
  let description: string;
  if (company.description_short) {
    description = company.description_short;
  } else {
    const parts: string[] = [];
    if (company.industry_group) parts.push(company.industry_group);
    if (company.hq_city || company.hq_state) {
      parts.push(formatLocation(company.hq_city, company.hq_state));
    }
    description =
      parts.length > 0
        ? `${company.name}, ${parts.join(", ")}`
        : `${company.name} — company information and funding history.`;
  }

  return {
    title: `${company.name} — nous`,
    description,
  };
}

/** Render-friendly hostname for a website URL — strips protocol, "www.", and
 * trailing slash. Returns null on a malformed URL so the caller can fall back
 * to showing the raw string. */
function websiteHostname(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const host = new URL(url).host.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return null;
  }
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function CompanyPage({ params }: Props) {
  const { slug } = await params;
  const detail = await getCompanyBySlug(slug);

  if (!detail) {
    notFound();
  }

  const { company, people, fundingRounds, competitors, investors, news } = detail;

  // ── M3 key-facts derivations ──────────────────────────────────────────────
  // totalRaised = sum of non-null amount_raised across all funding rounds.
  // Sources = count of distinct primary_news_url across funding rounds.
  // Both fall back to "—" when there are zero rounds (never fabricate).
  const totalRaisedAmount = fundingRounds.reduce<number>((acc, r) => {
    return r.amount_raised != null ? acc + Number(r.amount_raised) : acc;
  }, 0);
  const hasAnyRaised = fundingRounds.some((r) => r.amount_raised != null);
  const distinctNewsSources = new Set(
    fundingRounds
      .map((r) => r.primary_news_url)
      .filter((u): u is string => u !== null),
  );

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {/* ── Company header ─────────────────────────────────────────────── */}
      <header className="mb-10">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-4xl font-semibold tracking-tight text-ink">
            {company.name}
          </h1>
          {/* Discovery badge — every company has a discovered_via value
              ('vc_portfolio' | 'news' | 'techcrunch'), surfacing how nous
              first found the company. */}
          <span
            className="rounded border border-edge px-2 py-0.5 text-xs text-ink-muted"
            title="How nous first discovered this company"
          >
            Discovered via {company.discovered_via}
          </span>
        </div>

        <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-2 text-sm text-ink-muted">
          {company.website && (
            <div>
              <dt className="sr-only">Website</dt>
              <dd>
                <a
                  href={company.website}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-ink-soft hover:text-ink underline underline-offset-2 decoration-ink-faint"
                >
                  {websiteHostname(company.website) ?? company.website}
                </a>
              </dd>
            </div>
          )}
          {(company.hq_city || company.hq_state) && (
            <div>
              <dt className="sr-only">Location</dt>
              <dd>{formatLocation(company.hq_city, company.hq_state)}</dd>
            </div>
          )}
          {company.year_incorporated && (
            <div>
              <dt className="sr-only">Year incorporated</dt>
              <dd>Est. {company.year_incorporated}</dd>
            </div>
          )}
          {company.industry_group && (
            <div>
              <dt className="sr-only">Industry</dt>
              <dd>{company.industry_group}</dd>
            </div>
          )}
          {/* Employee estimate (M5). Gated on a non-null min so it renders
              nothing until the estimate-employees stage populates the fields;
              source is attributed in the title per spec §11. */}
          {company.employee_count_min != null && (
            <div>
              <dt className="sr-only">Employees</dt>
              <dd
                title={
                  company.employee_count_source
                    ? `Source: ${company.employee_count_source}`
                    : undefined
                }
              >
                {formatEmployeeRange(
                  company.employee_count_min,
                  company.employee_count_max,
                )}{" "}
                employees
              </dd>
            </div>
          )}
        </dl>

        {/* Tagline — description_short as a muted paragraph below the meta strip */}
        {company.description_short && (
          <p className="mt-5 text-base text-ink-soft leading-relaxed max-w-2xl">
            {company.description_short}
          </p>
        )}

        {/* M3 key-facts strip — anchors the page with a tangible "total raised"
            number sourced from public news, attributed inline. Per spec §11,
            unattributed numbers are forbidden; "from N news sources" makes the
            attribution visible at a glance. */}
        <dl className="mt-6 flex flex-wrap gap-x-10 gap-y-3 text-sm">
          <div>
            <dt className="text-xs font-medium uppercase tracking-wider text-ink-muted">
              Total raised
            </dt>
            <dd
              className={`mt-1 text-base font-semibold ${hasAnyRaised ? "font-mono text-money" : "text-ink-faint"}`}
            >
              {hasAnyRaised ? formatUsd(totalRaisedAmount) : "—"}
            </dd>
            {fundingRounds.length > 0 && (
              <dd className="text-xs text-ink-muted">
                from {distinctNewsSources.size}{" "}
                {distinctNewsSources.size === 1
                  ? "news source"
                  : "news sources"}
              </dd>
            )}
          </div>
        </dl>
      </header>

      {/* ── About ──────────────────────────────────────────────────────── */}
      {company.description_long && (
        <section className="mb-12">
          <h2 className="text-lg font-semibold text-ink mb-4">About</h2>
          <Markdown>{company.description_long}</Markdown>
        </section>
      )}

      {/* ── Category + Tags ────────────────────────────────────────────── */}
      {(company.primary_category || (company.tags && company.tags.length > 0)) && (
        <section className="mb-10">
          {company.primary_category && (
            <p className="text-xs font-medium uppercase tracking-wider text-ink-muted mb-3">
              {company.primary_category}
            </p>
          )}
          {company.tags && company.tags.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {company.tags.map((tag) => (
                <span
                  key={tag}
                  className="rounded-full border border-edge px-2.5 py-0.5 text-xs text-ink-soft"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </section>
      )}

      {/* ── Leadership / founders (from the company website) ───────────── */}
      <Team people={people} companyName={company.name} />

      {/* ── Funding history (M3) ───────────────────────────────────────── */}
      <FundingHistory rounds={fundingRounds} />

      {/* ── Investors ──────────────────────────────────────────────────── */}
      <Investors investors={investors} rounds={fundingRounds} />

      {/* ── Competitors (M4) ───────────────────────────────────────────── */}
      <Competitors competitors={competitors} />

      {/* ── News ───────────────────────────────────────────────────────── */}
      <News news={news} />
    </main>
  );
}
