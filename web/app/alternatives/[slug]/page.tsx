// "Top alternatives to {Company}" — a high-SEO-value landing page built from
// the company's competitor edges. Resolved competitors (matched to an indexed
// company) render as linked CompanyCards; LLM-named ones render as text with
// their rationale. Server component: all data flows in from getAlternatives.

// Revalidate every 6 hours, matching the company pages' ISR window.
export const revalidate = 21600;

import type { Metadata } from "next";
import Link from "next/link";
import { notFound, permanentRedirect } from "next/navigation";
import { getAliasTargetSlug, getAlternatives } from "@/lib/queries";
import type { AlternativesData } from "@/lib/types";
import { CompanyCard } from "@/components/CompanyCard";
import { JsonLd } from "@/components/JsonLd";
import { siteOrigin } from "@/lib/site";
import { vsPath } from "@/lib/vs";

type Props = {
  params: Promise<{ slug: string }>;
};

// ─── Metadata ─────────────────────────────────────────────────────────────────

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const data = await getAlternatives(slug);

  if (!data) {
    // The layout's title template appends " — nous".
    return { title: "Alternatives not found" };
  }

  const { company } = data;
  const count = data.resolved.length + data.named.length;

  // Description leans on the company's own one-liner when present, so the page
  // reads as real content rather than a templated doorway.
  const lead = company.description_short
    ? `${company.name} — ${company.description_short}`
    : `${company.name}`;
  const description =
    count > 0
      ? `${count} alternatives and competitors to ${company.name}, with what each one does. ${lead}`
      : `Alternatives and competitors to ${company.name}. ${lead}`;

  return {
    // Bare title — the layout's template appends " — nous".
    title: `Top alternatives to ${company.name}`,
    description,
    alternates: { canonical: `/alternatives/${slug}` },
  };
}

/**
 * schema.org ItemList of the alternatives, in rank order. Resolved competitors
 * point at their nous page (absolute URL); named-only ones carry just a name.
 * No-fabrication rule applies: we emit only what we hold.
 */
function alternativesJsonLd(data: AlternativesData): Record<string, unknown> {
  const origin = siteOrigin();
  // Interleave is unnecessary — present resolved first (they're richer), then
  // named, each already rank-sorted by the query. position is 1-based and
  // contiguous across the whole list.
  const items: Record<string, unknown>[] = [];
  let position = 1;

  for (const c of data.resolved) {
    items.push({
      "@type": "ListItem",
      position: position++,
      url: `${origin}/c/${c.slug}`,
      name: c.name,
    });
  }
  for (const c of data.named) {
    items.push({
      "@type": "ListItem",
      position: position++,
      name: c.name,
    });
  }

  return {
    "@context": "https://schema.org",
    "@type": "ItemList",
    name: `Alternatives to ${data.company.name}`,
    numberOfItems: items.length,
    itemListElement: items,
  };
}

/** Human-readable provenance line for a competitor edge, mirroring the
 * Competitors component's wording (TechCrunch-grounded vs LLM-inferred). */
function sourceLabel(source: string): string {
  return source === "techcrunch"
    ? "named in TechCrunch coverage"
    : "potential competitor (AI-inferred)";
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function AlternativesPage({ params }: Props) {
  const { slug } = await params;
  const data = await getAlternatives(slug);

  // Unknown or excluded company → 404. (An existing company with zero
  // competitors is NOT null — it renders the graceful empty state below.)
  if (!data) {
    // Same miss-path alias redirect as /c/[slug]: a merged-away slug 308s to
    // the survivor's alternatives page. Lookup only on miss — valid slugs pay
    // zero extra queries. See the /c/[slug] page for the loop-guard rationale.
    const target = await getAliasTargetSlug(slug);
    if (target && target !== slug) {
      permanentRedirect(`/alternatives/${target}`);
    }
    notFound();
  }

  const { company, resolved, named } = data;
  const total = resolved.length + named.length;

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      {total > 0 && <JsonLd data={alternativesJsonLd(data)} />}

      {/* ── Header ────────────────────────────────────────────────────────── */}
      <header className="mb-10">
        <p className="mb-2 text-sm text-ink-muted">
          <Link
            href={`/c/${company.slug}`}
            className="hover:text-ink underline underline-offset-2 decoration-ink-faint"
          >
            ← {company.name}
          </Link>
        </p>
        <h1 className="text-4xl font-semibold tracking-tight text-ink">
          Top alternatives to {company.name}
        </h1>
        {company.description_short && (
          <p className="mt-4 max-w-2xl text-base text-ink-soft leading-relaxed">
            {company.description_short}
          </p>
        )}
        {total > 0 && (
          <p className="mt-4 text-sm text-ink-muted">
            {total} {total === 1 ? "alternative" : "alternatives"} and competitors
            {company.industry_group ? ` in ${company.industry_group}` : ""}, with
            what each one does.
          </p>
        )}
      </header>

      {/* ── Empty state ───────────────────────────────────────────────────────
          The company exists but we haven't recorded competitors yet. We render
          a graceful note (and link back to the profile) rather than 404 — the
          page may fill in after a later pipeline run. ─────────────────────── */}
      {total === 0 && (
        <div className="rounded-lg border border-dashed border-edge px-8 py-10">
          <p className="text-sm text-ink-muted">
            We haven&apos;t recorded any competitors for{" "}
            <span className="font-medium text-ink-soft">{company.name}</span> yet.
            See the{" "}
            <Link
              href={`/c/${company.slug}`}
              className="underline underline-offset-2 hover:text-ink"
            >
              full profile
            </Link>{" "}
            or{" "}
            <Link
              href="/companies"
              className="underline underline-offset-2 hover:text-ink"
            >
              browse the catalog
            </Link>
            .
          </p>
        </div>
      )}

      {/* ── Resolved alternatives (linked cards) ──────────────────────────── */}
      {resolved.length > 0 && (
        <section className="mb-12">
          <h2 className="text-lg font-semibold text-ink mb-4">
            Alternatives on nous
          </h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {resolved.map((c) => (
              <div key={c.slug}>
                <CompanyCard company={c} logoUrl={c.logo_url} />
                {/* Why nous lists this as an alternative — the competitor
                    rationale, captioned with its provenance. Every surfaced
                    relationship carries a visible source (spec §11). */}
                {(c.reasoning || c.description) && (
                  <p className="mt-2 px-1 text-xs text-ink-muted leading-snug">
                    {c.reasoning || c.description}
                  </p>
                )}
                <p className="mt-1 px-1 font-mono text-xs text-ink-faint">
                  {c.source === "techcrunch" && c.source_url ? (
                    <a
                      href={c.source_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline underline-offset-2 hover:text-ink-soft"
                    >
                      {sourceLabel(c.source)}
                    </a>
                  ) : (
                    sourceLabel(c.source)
                  )}
                </p>
                {/* Head-to-head compare link — the crawl path to the /vs pair
                    (indexable only when it's a funded competitor edge). */}
                <p className="mt-1 px-1 text-xs">
                  <Link
                    href={vsPath(company.slug, c.slug)}
                    className="text-accent underline underline-offset-2 decoration-accent/40 hover:decoration-accent"
                  >
                    Compare {company.name} vs {c.name} →
                  </Link>
                </p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ── Named-only alternatives (no nous page) ────────────────────────── */}
      {named.length > 0 && (
        <section className="mb-12">
          <h2 className="text-lg font-semibold text-ink mb-4">
            Other named competitors
          </h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {named.map((c, i) => (
              <article
                key={`${c.name}-${i}`}
                className="rounded-lg border border-edge p-4"
              >
                <header className="flex items-baseline gap-2">
                  <span className="font-semibold text-ink">{c.name}</span>
                  <span className="ml-auto text-xs text-ink-faint">#{c.rank}</span>
                </header>

                {c.source === "techcrunch" && c.source_url ? (
                  <a
                    href={c.source_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-2 inline-block font-mono text-xs text-ink-muted underline underline-offset-2 hover:text-ink-soft"
                  >
                    {sourceLabel(c.source)}
                  </a>
                ) : (
                  <span className="mt-2 inline-block font-mono text-xs text-ink-muted">
                    {sourceLabel(c.source)}
                  </span>
                )}

                {c.description && (
                  <p className="mt-2 text-sm text-ink-soft leading-snug">
                    {c.description}
                  </p>
                )}
                {c.reasoning && (
                  <p className="mt-2 text-xs text-ink-muted leading-snug">
                    <span className="font-medium">Why they compete: </span>
                    {c.reasoning}
                  </p>
                )}
              </article>
            ))}
          </div>
        </section>
      )}

      {/* Back link to the full company profile (also present in the header). */}
      <p className="text-sm text-ink-muted">
        <Link
          href={`/c/${company.slug}`}
          className="hover:text-ink underline underline-offset-2 decoration-ink-faint"
        >
          View the full {company.name} profile →
        </Link>
      </p>
    </main>
  );
}
