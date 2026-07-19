// Revalidate every 6 hours per spec §7.5.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { notFound, permanentRedirect } from "next/navigation";
import {
  getAliasTargetSlug,
  getAlsoBackedBy,
  getCareerMoves,
  getCompanyBySlug,
  getInvestorNameToSlugMap,
  getRelatedCompanies,
  getSimilarCompanies,
} from "@/lib/queries";
import type {
  CompanyRow,
  FundingRoundWithInvestors,
  PersonRow,
} from "@/lib/types";
import {
  discoveredViaLabel,
  formatDate,
  formatEmployeeRange,
  formatLocation,
  formatUsd,
  formatUsdExact,
} from "@/lib/format";
import { computeTotalRaised } from "@/lib/funding";
import { repoIssueUrl, siteOrigin } from "@/lib/site";
import { CompanyLogo } from "@/components/CompanyCard";
import { JsonLd } from "@/components/JsonLd";
import { Markdown } from "@/components/Markdown";
import { StatusBadge } from "@/components/StatusBadge";
import { MomentumBadge } from "@/components/MomentumBadge";
import { Team } from "@/components/Team";
import { FounderBackground } from "@/components/FounderBackground";
import { FundingTimeline, type FundingItem } from "@/components/FundingTimeline";
import { NewsSection, type NewsItem } from "@/components/NewsSection";
import { buildTimeline } from "@/lib/timeline";
import { Investors } from "@/components/Investors";
import { Competitors } from "@/components/Competitors";
import { RelatedCompanies } from "@/components/RelatedCompanies";
import { RssLink } from "@/components/RssLink";
import { ProvenancePanel } from "@/components/ProvenancePanel";
import { SourceLink } from "@/components/SourceLink";
import { VerifiedBadge } from "@/components/VerifiedBadge";
import { buildVerificationLookup, verifiedAgainst } from "@/lib/verifications";
import { Sources, hasRenderableCitations } from "@/components/Sources";

// At or above this many consecutive failed homepage scrapes, the detail page
// shows a muted "possibly inactive" rider. Deliberately a low-confidence
// heuristic — rendered as quiet text, not a badge (see the header below).
const INACTIVE_FAILURE_THRESHOLD = 3;

// ─── Types ────────────────────────────────────────────────────────────────────

type Props = {
  params: Promise<{ slug: string }>;
};

// ─── Metadata ─────────────────────────────────────────────────────────────────

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const detail = await getCompanyBySlug(slug);

  if (!detail) {
    // The layout's title template appends " — nous".
    return { title: "Company not found" };
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
    // Bare company name — the layout's title template appends " — nous".
    title: company.name,
    description,
    alternates: {
      canonical: `/c/${slug}`,
      // Per-company RSS auto-discovery (overrides the layout's global feed
      // alternate on this page — the company feed is the relevant one here).
      types: {
        "application/rss+xml": [
          {
            url: `/c/${slug}/feed.xml`,
            title: `${company.name} — funding & news (RSS)`,
          },
        ],
        // The page's markdown sibling for AI answer engines (llms.txt
        // convention): same facts, per-fact source URLs inline.
        "text/markdown": [
          {
            url: `/c/${slug}.md`,
            title: `${company.name} — sourced facts (markdown)`,
          },
        ],
      },
    },
  };
}

/**
 * schema.org Organization markup for the company. Only fields we actually
 * hold are emitted — the project's no-fabrication rule applies to structured
 * data too, so an unknown value means the property is absent, never guessed.
 */
function companyJsonLd(company: CompanyRow): Record<string, unknown> {
  const org: Record<string, unknown> = {
    "@context": "https://schema.org",
    "@type": "Organization",
    name: company.name,
  };

  if (company.website) org.url = company.website;
  if (company.description_short) org.description = company.description_short;

  if (company.hq_city || company.hq_state) {
    const address: Record<string, unknown> = { "@type": "PostalAddress" };
    if (company.hq_city) address.addressLocality = company.hq_city;
    if (company.hq_state) address.addressRegion = company.hq_state;
    org.address = address;
  }

  if (company.year_incorporated != null) {
    org.foundingDate = String(company.year_incorporated); // "YYYY"
  }

  if (
    company.employee_count_min != null ||
    company.employee_count_max != null
  ) {
    const employees: Record<string, unknown> = {
      "@type": "QuantitativeValue",
    };
    if (company.employee_count_min != null) {
      employees.minValue = company.employee_count_min;
    }
    if (company.employee_count_max != null) {
      employees.maxValue = company.employee_count_max;
    }
    org.numberOfEmployees = employees;
  }

  return org;
}

/** Roles that mark a person as a founder, for the "Who founded …" FAQ answer.
 * Matches "founder", "co-founder", "founding <X>", etc. We only claim someone
 * founded the company when their recorded role actually says so — never infer
 * a founder from a generic executive title (no-fabrication rule). */
const FOUNDER_ROLE = /found(s|ed|er|ing)?\b/i;

/**
 * schema.org FAQPage markup for AI answer engines / rich results. Each Q&A is
 * derived purely from data already shown on the page, and a question is emitted
 * only when its answer exists (no fabrication). Returns null when none of the
 * four questions can be answered, so the caller can skip the block entirely.
 *
 * Questions:
 *   - "What does {Company} do?"        ← description_short
 *   - "Who founded {Company}?"         ← people whose role marks them a founder
 *   - "How much has {Company} raised?" ← the displayed total raised (when known)
 *   - "Where is {Company} based?"      ← HQ city/state
 */
function companyFaqJsonLd(
  company: CompanyRow,
  people: PersonRow[],
  raised: { has: boolean; display: number },
): Record<string, unknown> | null {
  const qa: { q: string; a: string }[] = [];

  if (company.description_short) {
    qa.push({
      q: `What does ${company.name} do?`,
      a: company.description_short,
    });
  }

  const founders = people.filter((p) => FOUNDER_ROLE.test(p.role));
  if (founders.length > 0) {
    const names = founders.map((p) => p.name);
    const list =
      names.length === 1
        ? names[0]
        : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
    qa.push({
      q: `Who founded ${company.name}?`,
      a: `${company.name} was founded by ${list}.`,
    });
  }

  if (raised.has) {
    qa.push({
      q: `How much has ${company.name} raised?`,
      a: `${company.name} has raised ${formatUsd(raised.display)} in disclosed funding to date.`,
    });
  }

  if (company.hq_city || company.hq_state) {
    qa.push({
      q: `Where is ${company.name} based?`,
      a: `${company.name} is based in ${formatLocation(company.hq_city, company.hq_state)}.`,
    });
  }

  if (qa.length === 0) return null;

  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: qa.map(({ q, a }) => ({
      "@type": "Question",
      name: q,
      acceptedAnswer: { "@type": "Answer", text: a },
    })),
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

/** True when the value parses as an http(s) URL. The `valuation_source` column
 * sometimes holds a publisher NAME (e.g. "TechCrunch") rather than a link, so
 * citation-building uses this to decide whether to cite it directly or fall
 * back to the round's article URL. */
function isHttpUrl(value: string | null | undefined): value is string {
  if (!value) return false;
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

/** Human-readable display label for a non-active company status, mirroring the
 * StatusBadge labels. Falls back to the raw value for unknown statuses. */
function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    acquired: "Acquired",
    shut_down: "Shut down",
    ipo: "IPO",
  };
  return labels[status] ?? status;
}

/** Sources-list label for a funding round: "<round type> · <amount>", degrading
 * gracefully when either piece is missing (round type alone, amount alone, or a
 * generic "Funding round" when both are absent). The cited host is appended by
 * the Sources component, so this is the fact description only. */
function fundingRoundLabel(round: FundingRoundWithInvestors): string {
  const type = round.round_type?.trim() || null;
  const amount = round.amount_raised != null ? formatUsd(round.amount_raised) : null;
  if (type && amount) return `${type} · ${amount}`;
  if (type) return type;
  if (amount) return `Funding · ${amount}`;
  return "Funding round";
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function CompanyPage({ params }: Props) {
  const { slug } = await params;
  const [detail, investorNameToSlug] = await Promise.all([
    getCompanyBySlug(slug),
    getInvestorNameToSlugMap(),
  ]);

  if (!detail) {
    // Miss path only (valid slugs pay zero extra queries): a merged-away
    // company's slug lives on in slug_aliases, so a dead URL 308s to the
    // survivor's current slug instead of burning the link. An excluded
    // survivor still redirects — the target page's own exclusion logic 404s
    // (truthful: the alias exists, the destination is gone). The
    // target !== slug guard is write-time-guaranteed (a live slug is never an
    // alias); kept as defense-in-depth against a redirect loop.
    const target = await getAliasTargetSlug(slug);
    if (target && target !== slug) {
      permanentRedirect(`/c/${target}`);
    }
    notFound();
  }

  const { company, people, fundingRounds, competitors, investors, news, verifications } =
    detail;
  // "✓ Verified against source" — supported verdicts keyed by fact (empty until
  // the verify-sources apply pass runs; degrades to no badges).
  const vlookup = buildVerificationLookup(verifications);
  // Funding + news, assembled ONCE and split by kind below (FundingTimeline /
  // NewsSection take pre-split items — no double computation).
  const timelineItems = buildTimeline(fundingRounds, news);

  // Relationship-graph fetches depend on the resolved company id, so they run
  // after getCompanyBySlug — but in parallel with each other (same idiom as the
  // detail fan-out). All degrade to [] on missing env / error, so the section
  // simply renders nothing when there's no graph data yet. The embedding
  // neighbors (getSimilarCompanies) replace the heuristic 'similar' edges in
  // the UI when the company has an embedding; the heuristic list stays the
  // fallback for not-yet-embedded companies.
  const [similar, similarByDescription, alsoBackedBy, careerMoves] =
    await Promise.all([
      getRelatedCompanies(company.id),
      getSimilarCompanies(company.id),
      getAlsoBackedBy(company.id),
      getCareerMoves(company.id),
    ]);

  // ── M3 key-facts derivations ──────────────────────────────────────────────
  // Hybrid "total raised": computed = sum of non-null amount_raised across all
  // rounds; stated = an article-stated cumulative total recorded by the
  // pipeline (news discovery never backfills historical rounds, so the sum
  // undercounts companies with a pre-nous funding history). The tile shows
  // max(stated, computed). Falls back to "—" when neither exists (never
  // fabricate).
  // The total_raised_* fields may be undefined (prod rows predate migration
  // 0021 until it runs there; select("*") omits unknown columns) — normalize
  // through `?? null` / Number() so the computed path renders unharmed.
  //
  // Duplicate-round defense + max(stated, computed) both live in the shared
  // helper (lib/funding.ts) so this tile, the OG card, and the compare table
  // can never disagree — see computeTotalRaised for the full invariant
  // (dedup on (round_type, amount_raised); Helion's $465M Series G once
  // summed to $2.3B across 5 duplicate rows).
  const {
    total: displayedTotal,
    statedWins,
    hasTotal: hasTotalRaised,
  } = computeTotalRaised(company.total_raised_usd ?? null, fundingRounds);
  const statedAsOf = company.total_raised_as_of ?? null;

  // FAQPage structured data (no visible UI) for AI answer engines / rich
  // results, built from data already on the page. Null when none of its
  // questions can be answered, so the block is skipped entirely.
  const faqJsonLd = companyFaqJsonLd(company, people, {
    has: hasTotalRaised,
    display: displayedTotal,
  });
  // Citation for the displayed total: the stated figure's source article when
  // that figure wins; otherwise the company's own site is the citation for the
  // summed-from-rounds total (a self-reported aggregate). Per-round article
  // URLs already appear as their own Sources entries. Per the locked rule, a
  // self-reported figure cites the company's website (fallback when the stated
  // source URL is null).
  const totalRaisedSourceUrl = statedWins
    ? (company.total_raised_source_url ?? company.website ?? null)
    : (company.website ?? null);

  // ── D2: latest-valuation tile ──────────────────────────────────────────────
  // The most recent round (fundingRounds is already sorted announced_date desc,
  // nulls last) that carries a post-money valuation. The tile is hidden when no
  // round has one; its source is folded into the Sources section below.
  const valuationRound =
    fundingRounds.find((r) => r.valuation_post_money != null) ?? null;

  // ── Sources (D1): one labeled citation per fact shown on the page ──────────
  // Order follows the page's prominence: the header tiles (total raised, then
  // the latest valuation) first, then each funding-history row (its article and
  // its valuation), then leadership, then company status. The Sources component
  // de-dupes identical URLs (first label wins, so a header-fact label survives
  // when several facts share one article) and drops unparseable ones. Every
  // rendered fact's provenance lives here — the cited host stands alone (no
  // "self-reported" wording); a company-domain URL signals a self-reported
  // figure on its own. A funding round's valuation_source is sometimes a
  // publisher NAME ("TechCrunch") rather than a link, so we cite it only when
  // it parses as a URL and otherwise fall back to the round's article URL —
  // the article that reported the round also reported its valuation.
  const citations: { label: string; url: string }[] = [];

  if (hasTotalRaised && totalRaisedSourceUrl) {
    citations.push({
      label: `Total raised · ${formatUsd(displayedTotal)}`,
      url: totalRaisedSourceUrl,
    });
  }
  if (valuationRound?.valuation_post_money != null) {
    const valUrl = isHttpUrl(valuationRound.valuation_source)
      ? valuationRound.valuation_source
      : (valuationRound.primary_news_url ?? null);
    if (valUrl) {
      citations.push({
        label: `Latest valuation · ${formatUsd(valuationRound.valuation_post_money)} post-money`,
        url: valUrl,
      });
    }
  }
  for (const round of fundingRounds) {
    const roundLabel = fundingRoundLabel(round);
    if (round.primary_news_url) {
      citations.push({ label: roundLabel, url: round.primary_news_url });
    }
    if (round.valuation_post_money != null) {
      const valUrl = isHttpUrl(round.valuation_source)
        ? round.valuation_source
        : (round.primary_news_url ?? null);
      if (valUrl) {
        citations.push({
          label: `${roundLabel} · ${formatUsd(round.valuation_post_money)} valuation`,
          url: valUrl,
        });
      }
    }
  }
  // Leadership: every person carries the same website source_url; cite once.
  const teamSourceUrl = people.find((p) => p.source_url)?.source_url ?? null;
  if (teamSourceUrl) {
    citations.push({ label: "Leadership", url: teamSourceUrl });
  }
  if (company.status !== "active" && company.status_source_url) {
    citations.push({
      label: `Status · ${statusLabel(company.status)}`,
      url: company.status_source_url,
    });
  }
  // Website provenance: how nous learned the company's site (a Wikidata entity
  // page / a sourcing article / a VC portfolio page). Cited like the other facts
  // — consistent with total-raised/status having both an inline superscript and a
  // Sources row — and it is what makes the "Website / Wikidata / VC portfolio"
  // source-type labels reachable (the label override keys on this URL's host).
  if (company.website_source_url) {
    citations.push({ label: "Website", url: company.website_source_url });
  }

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      <JsonLd data={companyJsonLd(company)} />
      {/* FAQ structured data (JSON-LD only — no visible UI). Answers, from data
          already on the page: what the company does, who founded it, how much
          it raised, and where it's based — for AI answer engines. */}
      {faqJsonLd && <JsonLd data={faqJsonLd} />}
      {/* ── Company header ─────────────────────────────────────────────── */}
      <header className="mb-10">
        <div className="flex flex-wrap items-center gap-3">
          {/* Company logo (or monogram fallback) beside the H1. logo_url is an
              external favicon URL that the pipeline backfills; until then the
              fallback keeps the header's left edge stable. */}
          <CompanyLogo logoUrl={company.logo_url} name={company.name} size={44} />
          <h1 className="text-4xl font-semibold tracking-tight text-ink">
            {company.name}
          </h1>
          {/* Status badge — renders nothing while status='active'; otherwise
              marks the exit (Acquired / Shut down / IPO), linking to the
              announcement when a source URL was recorded. The badge's own link
              is visually undiscoverable, so a subtle source superscript beside
              it makes the provenance visible and consistent with the other
              sourced figures (self-omits when status_source_url is
              absent/unparseable). Grouped so the superscript hugs the badge
              rather than picking up the header row's wider gap. */}
          {company.status !== "active" ? (
            <span className="inline-flex items-center gap-0.5">
              <StatusBadge
                status={company.status}
                sourceUrl={company.status_source_url}
              />
              <SourceLink url={company.status_source_url} label="Status" />
              <VerifiedBadge
                verification={verifiedAgainst(
                  vlookup,
                  "status",
                  "",
                  company.status_source_url,
                  { kind: "status", status: company.status },
                )}
                label="Status"
              />
            </span>
          ) : (
            <StatusBadge
              status={company.status}
              sourceUrl={company.status_source_url}
            />
          )}
          {/* Momentum badge — a "🔥 Heating up" pill for high-momentum
              companies. Renders null below the threshold and whenever the
              momentum_score column is absent pre-migration (undefined →
              isHeatingUp false), so it degrades cleanly without a query change:
              CompanyRow uses select("*"), so the column arrives automatically
              once migration 0039 lands. */}
          <MomentumBadge score={company.momentum_score} />
          {/* Discovery badge — every company has a discovered_via value
              ('vc_portfolio' | 'news' | 'techcrunch'), surfacing how nous
              first found the company. Humanized so the raw enum never leaks. */}
          <span
            className="rounded border border-edge px-2 py-0.5 text-xs text-ink-muted"
            title="How nous first discovered this company"
          >
            Discovered via {discoveredViaLabel(company.discovered_via)}
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
                {/* Source for HOW nous learned the website (the Wikidata entity
                    page / sourcing article / VC portfolio page) — distinct from
                    the site link above. Self-omits when website_source_url is
                    absent/unparseable. */}
                <SourceLink url={company.website_source_url} label="Website" />
              </dd>
            </div>
          )}
          {(company.hq_city || company.hq_state) && (
            <div>
              <dt className="sr-only">Location</dt>
              <dd>
                {company.hq_state ? (
                  // Link the whole location string to the state page when a
                  // state is known. City-only rows stay plain text.
                  <Link
                    href={`/location/${encodeURIComponent(company.hq_state)}`}
                    className="hover:underline underline-offset-2"
                  >
                    {formatLocation(company.hq_city, company.hq_state)}
                  </Link>
                ) : (
                  formatLocation(company.hq_city, company.hq_state)
                )}
              </dd>
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
          {/* Freshness rider — when the enrichment pipeline last touched this
              profile. Hidden until last_enriched_at is populated. */}
          {company.last_enriched_at && (
            <div>
              <dt className="sr-only">Profile last updated</dt>
              <dd>Profile updated {formatDate(company.last_enriched_at)}</dd>
            </div>
          )}
        </dl>

        {/* Feed autodiscovery link — subscribe to this company's funding + news
            without an account (mirrors the <link rel="alternate"> in <head>). */}
        <div className="mt-4">
          <RssLink
            href={`/c/${company.slug}/feed.xml`}
            label="Follow via RSS"
            title={`Subscribe to ${company.name} funding & news (RSS)`}
          />
        </div>

        {/* Possibly-inactive rider — surfaced when the scraper has failed to
            reach the homepage on several consecutive runs. This is a
            low-confidence heuristic (a site can be down transiently or block
            our scraper), so it renders as quiet plain text, intentionally
            quieter than the StatusBadge pill above. */}
        {company.consecutive_scrape_failures >= INACTIVE_FAILURE_THRESHOLD && (
          <p className="mt-3 text-xs text-ink-muted">
            Possibly inactive — site unreachable on recent checks
          </p>
        )}

        {/* Tagline — description_short as a muted paragraph below the meta strip */}
        {company.description_short && (
          <p className="mt-5 text-base text-ink-soft leading-relaxed max-w-2xl">
            {company.description_short}
          </p>
        )}

        {/* M3 key-facts strip — anchors the page with a tangible "total raised"
            number and, when known, the latest post-money valuation. Provenance
            for both lives in the consolidated Sources section at the bottom
            (spec §11: every fact carries a visible source); only the "as of"
            freshness riders stay inline here. */}
        <dl className="mt-6 flex flex-wrap gap-x-10 gap-y-3 text-sm">
          <div>
            <dt className="text-xs font-medium uppercase tracking-wider text-ink-muted">
              Total raised
            </dt>
            <dd
              className={`mt-1 text-base font-semibold ${hasTotalRaised ? "font-mono text-money" : "text-ink-faint"}`}
              // Hovering the rounded figure reveals the exact dollars.
              title={hasTotalRaised ? formatUsdExact(displayedTotal) : undefined}
            >
              {hasTotalRaised ? formatUsd(displayedTotal) : "—"}
              {/* Inline source affordance for the displayed total. Self-omits
                  when the source URL is absent/unparseable (e.g. a scheme-less
                  company.website fallback), so "—" never sprouts a dead link. */}
              {hasTotalRaised && (
                <SourceLink url={totalRaisedSourceUrl} label="Total raised" />
              )}
              {hasTotalRaised && (
                <VerifiedBadge
                  verification={verifiedAgainst(
                    vlookup,
                    "total_raised",
                    "",
                    totalRaisedSourceUrl,
                    { kind: "amount", amountUsd: displayedTotal },
                  )}
                  label="Total raised"
                />
              )}
            </dd>
            {/* "As of" freshness rider (not a citation) — kept inline. */}
            {hasTotalRaised && statedAsOf && (
              <dd className="text-xs text-ink-muted">
                as of {formatDate(statedAsOf)}
              </dd>
            )}
          </div>

          {/* D2: latest-valuation tile — most recent round carrying a
              post-money valuation, to the right of total raised. Hidden
              entirely when no round has a valuation; its source is in the
              Sources section. */}
          {valuationRound?.valuation_post_money != null && (
            <div>
              <dt className="text-xs font-medium uppercase tracking-wider text-ink-muted">
                Latest valuation
              </dt>
              <dd className="mt-1 text-base font-semibold font-mono text-money">
                <span title={formatUsdExact(valuationRound.valuation_post_money)}>
                  {formatUsd(valuationRound.valuation_post_money)}
                </span>
                <span className="ml-1 font-sans text-xs font-normal text-ink-muted">
                  post-money
                </span>
              </dd>
              {valuationRound.announced_date && (
                <dd className="text-xs text-ink-muted">
                  as of {formatDate(valuationRound.announced_date)}
                </dd>
              )}
            </div>
          )}
        </dl>
      </header>

      {/* ── Husk placeholder (Task 1.5) ────────────────────────────────────
          Shown only for a true husk: discovered by the pipeline but not yet
          enriched AND carrying no other substance — no description, funding
          history, news, competitors, or investors. A company that has funding
          or news (even without a description) renders those sections instead,
          so we never claim "no profile yet" on a page full of data (e.g. a
          well-known company with a deep funding history). ──────────────────── */}
      {!company.description_long &&
        !company.description_short &&
        fundingRounds.length === 0 &&
        news.length === 0 &&
        competitors.length === 0 &&
        investors.length === 0 && (
          <div className="mb-12 rounded-lg border border-dashed border-edge px-8 py-10">
            <p className="text-sm text-ink-muted">
              We&apos;ve discovered{" "}
              <span className="text-ink-soft font-medium">{company.name}</span> via{" "}
              {discoveredViaLabel(company.discovered_via)} but haven&apos;t built a
              full profile yet. Check back after the next pipeline run.
            </p>
          </div>
        )}

      {/* ── About ──────────────────────────────────────────────────────── */}
      {company.description_long && (
        <section className="mb-12">
          <h2 className="text-lg font-semibold text-ink mb-4">About</h2>
          <Markdown>{company.description_long}</Markdown>
          {/* Honest attribution for the LLM-drafted summary. It's written by
              nous from several scraped pages of the company's own site, so we
              don't attribute it to a single source hostname (that would
              misrepresent a multi-page synthesis as one citation). The
              enrichment date is appended when known. Genuine per-fact source
              links (funding / news / people) live in the Sources section. */}
          <p className="mt-4 font-mono text-xs text-ink-muted">
            Summary written by nous from the company&apos;s website
            {company.last_enriched_at &&
              ` · ${formatDate(company.last_enriched_at)}`}
          </p>
        </section>
      )}

      {/* ── Leadership / founders (from the company website) ───────────── */}
      <Team people={people} />

      {/* ── Founder background: prior employers (talent-flow rider). Hidden
             until career_moves lands + is populated (migration-order-free). ── */}
      <FounderBackground careerMoves={careerMoves} />

      {/* ── Funding + In the news (owner-approved 2026-07-18 split of the old
             merged EventTimeline): the funding rail first — the page's spine —
             then standalone stories in a muted list beneath. buildTimeline runs
             ONCE and is split by kind; round-covering articles stay collapsed
             under their round, so every article appears exactly once. Each
             section omits itself when empty; the page owns the both-empty
             line. */}
      <FundingTimeline
        items={timelineItems.filter(
          (item): item is FundingItem => item.kind === "funding",
        )}
        verified={vlookup}
      />
      <NewsSection
        items={timelineItems.filter(
          (item): item is NewsItem => item.kind === "news",
        )}
      />
      {timelineItems.length === 0 && (
        <section className="mb-12">
          <p className="text-sm text-ink-muted">
            No funding rounds or news recorded yet.
          </p>
        </section>
      )}

      {/* ── Investors ──────────────────────────────────────────────────── */}
      <Investors
        investors={investors}
        rounds={fundingRounds}
        nameToSlug={investorNameToSlug}
      />

      {/* ── Competitors (M4) ───────────────────────────────────────────── */}
      {/* alternativesSlug renders a discreet link to /alternatives/[slug] in
          the section header (a higher-SEO-value standalone view of the same
          competitor set). */}
      <Competitors competitors={competitors} alternativesSlug={company.slug} />

      {/* ── Related companies (relationship graph) ─────────────────────── */}
      <RelatedCompanies
        similar={similar}
        similarByDescription={similarByDescription}
        alsoBackedBy={alsoBackedBy}
      />

      {/* ── Category + Tags (D3: moved to just before Sources) ─────────── */}
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
                <Link
                  key={tag}
                  href={`/tag/${encodeURIComponent(tag)}`}
                  className="rounded-full border border-edge px-2.5 py-0.5 text-xs text-ink-soft hover:border-ink-muted hover:text-ink transition-colors"
                >
                  {tag}
                </Link>
              ))}
            </div>
          )}
        </section>
      )}

      {/* ── Data & provenance (PR 2) ───────────────────────────────────────
          A trust-builder above the Sources list: a positive-only "documented"
          badge (gated on completeness_score, hidden below threshold — never a
          data-gap badge), a read-time "Last verified N days ago" freshness line,
          and a sourcing line anchor-linking down to the Sources section. Each
          part self-omits, and the panel renders nothing when none applies, so it
          degrades cleanly when the completeness/freshness columns are absent
          pre-migration (select("*") omits unknown columns). hasSources uses
          hasRenderableCitations — the SAME survival predicate <Sources> applies —
          so the sourcing line (and its #sources anchor) never shows when <Sources>
          would render nothing (a dead anchor + false trust claim). */}
      <ProvenancePanel
        company={company}
        hasSources={hasRenderableCitations(citations)}
      />

      {/* ── Sources (D1: consolidated, labeled, at the bottom) ─────────── */}
      {/* companyWebsite + website_source(_url) let Sources tag each citation with
          a muted source-type ("News / Website / Wikidata / VC portfolio"),
          inferred from the host with the website_source enum as ground truth;
          an un-inferable host stays unlabeled (never a guessed attribution). */}
      <Sources
        citations={citations}
        companyWebsite={company.website}
        websiteSource={company.website_source}
        websiteSourceUrl={company.website_source_url}
      />

      {/* ── Report incorrect data (company-scoped) ─────────────────────────
          The repo is public, so this prefilled GitHub-issue link resolves for
          visitors. Company-scoped: the issue title carries the name + slug and
          the body pre-fills this page's canonical URL plus a what's-wrong /
          correct-value / source skeleton. Additive to the site-wide footer
          "Report it" link — kept quiet (small, muted) beneath the Sources. */}
      <p className="text-xs text-ink-muted">
        Spotted something wrong on this page?{" "}
        <a
          href={repoIssueUrl(
            `Data correction: ${company.name} (${company.slug})`,
            [
              `Company: ${company.name}`,
              `Page: ${siteOrigin()}/c/${company.slug}`,
              "",
              "What's incorrect:",
              "",
              "Correct value:",
              "",
              "Source (URL):",
              "",
            ].join("\n"),
          )}
          target="_blank"
          rel="noopener noreferrer"
          className="underline underline-offset-2 decoration-ink-faint hover:text-ink"
        >
          Report incorrect data
        </a>
      </p>
    </main>
  );
}
