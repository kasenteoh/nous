// Server component — the consolidated "Sources" section at the bottom of
// /c/[slug]. Read-only display; the page assembles the citation list and passes
// it in via props.
//
// Project rule (spec §11): every fact rendered on a company page must carry a
// visible source. Rather than scatter inline "via {host}" links through the
// funding table, team list, and key-facts tiles, those citations are collected
// here into one labeled list. The cited URL stands alone — the hostname conveys
// provenance (independent press vs the company's own domain), so there is no
// "self-reported" / "company-stated" wording.

import { httpHost } from "@/lib/url";

interface Citation {
  /** Human-readable description of the fact being sourced, e.g. "Series B · $40M". */
  label: string;
  /** The source URL. Self-reported facts cite the company's own domain. */
  url: string;
}

/** Render-friendly hostname — strips protocol, "www.", and path. Returns null
 *  on a malformed URL so the caller can decide whether to keep the citation. */
function hostname(url: string): string | null {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return null;
  }
}

/** Like {@link hostname} but tolerant of a scheme-less value (the pipeline
 *  stores `company.website` as a bare domain like "acme.com"). Prefixes
 *  `https://` when no http(s) scheme is present so a company's own domain still
 *  resolves for the "Website" source-type match. */
function tolerantHost(value: string | null | undefined): string | null {
  if (!value) return null;
  const candidate = /^https?:\/\//i.test(value) ? value : `https://${value}`;
  return hostname(candidate);
}

/** The source-type label shown beside a citation's hostname. A closed set — an
 *  unknown host yields NO label rather than a guess (the moat forbids a wrong
 *  attribution). */
export type SourceType = "News" | "Website" | "Wikidata" | "VC portfolio";

/** DB-recorded ground truth: the pipeline's `website_source` enum mapped to a
 *  display label. This is the only reliable signal for a VC-portfolio page
 *  (whose host isn't otherwise inferable), so it overrides host inference for
 *  the website-provenance URL. */
const WEBSITE_SOURCE_LABELS: Record<string, SourceType> = {
  wikidata: "Wikidata",
  news_outbound: "News",
  vc_portfolio: "VC portfolio",
};

/** Conservative allowlist of genuine press / press-wire hosts. Matched exactly
 *  or as a parent domain (so `feeds.reuters.com` still counts). Kept small on
 *  purpose: a host we don't recognize gets no label, never a mislabel. */
const NEWS_HOSTS: readonly string[] = [
  "techcrunch.com",
  "forbes.com",
  "reuters.com",
  "bloomberg.com",
  "wsj.com",
  "nytimes.com",
  "fortune.com",
  "cnbc.com",
  "axios.com",
  "venturebeat.com",
  "theinformation.com",
  "businesswire.com",
  "prnewswire.com",
  "globenewswire.com",
  "finsmes.com",
  "geekwire.com",
  "pymnts.com",
];

function isNewsHost(host: string): boolean {
  return NEWS_HOSTS.some((h) => host === h || host.endsWith(`.${h}`));
}

/**
 * Infer a citation's source-type label from its host, with two ground-truth
 * overrides. Returns null when the type cannot be inferred CONFIDENTLY — an
 * unknown host is left unlabeled rather than guessed (the moat forbids a wrong
 * attribution).
 *
 * Precedence:
 *   1. The website-provenance URL (`websiteSourceUrl`) → its DB-recorded
 *      `website_source` type (the only reliable "VC portfolio" signal).
 *   2. `wikidata.org` → "Wikidata".
 *   3. The company's own domain (`companyHost`) → "Website" (self-reported
 *      figures and leadership cite the company's site).
 *   4. A known press host → "News".
 *   5. Otherwise → null (no label).
 */
export function citationSourceType(
  url: string,
  opts: {
    companyHost?: string | null;
    websiteSource?: string | null;
    websiteSourceUrl?: string | null;
  } = {},
): SourceType | null {
  // Only ever label a real web source. httpHost rejects non-http(s) / malformed
  // URLs (exotic schemes like ftp: still yield a hostname from a bare new URL()),
  // so an odd-scheme URL never gets a source-type tag.
  const host = httpHost(url);
  if (host === null) return null;

  if (
    opts.websiteSource &&
    opts.websiteSourceUrl &&
    httpHost(opts.websiteSourceUrl) === host
  ) {
    const mapped = WEBSITE_SOURCE_LABELS[opts.websiteSource];
    if (mapped) return mapped;
  }

  if (host === "wikidata.org" || host.endsWith(".wikidata.org")) {
    return "Wikidata";
  }
  if (opts.companyHost && host === opts.companyHost) return "Website";
  if (isNewsHost(host)) return "News";
  return null;
}

/**
 * True when at least one citation would actually render here — i.e. has a
 * parseable URL (a non-null {@link hostname}). `<Sources>` returns null when no
 * citation survives, so the ProvenancePanel gates its "every figure links to a
 * recorded source" line (and its `#sources` anchor) on this SAME predicate: a raw
 * `citations.length > 0` would show the line — and a dead anchor — for a company
 * whose only source URLs are unparseable (e.g. the scheme-less `company.website`
 * fallback), a false trust claim. Keep in lockstep with the survival filter below.
 */
export function hasRenderableCitations(
  citations: readonly { url: string }[],
): boolean {
  return citations.some((c) => hostname(c.url) !== null);
}

interface Props {
  /** Citations in display order. The component de-dupes identical URLs (first
   *  label wins) and drops any whose URL is unparseable. */
  citations: Citation[];
  /** The company's own website — used to label citations that cite the
   *  company's own domain as "Website". Scheme-less values are tolerated. */
  companyWebsite?: string | null;
  /** The pipeline's `website_source` enum ('wikidata' | 'news_outbound' |
   *  'vc_portfolio') and the URL it attributes, giving a ground-truth source
   *  type for the website-provenance citation. */
  websiteSource?: string | null;
  websiteSourceUrl?: string | null;
}

export function Sources({
  citations,
  companyWebsite,
  websiteSource,
  websiteSourceUrl,
}: Props) {
  const companyHost = tolerantHost(companyWebsite);
  // De-dupe so each row is visually distinct. The same article often sources
  // several facts (a round, its valuation, the running total), and a company
  // can have many rounds whose source URLs differ only by tracking query string
  // but render to the same "<label> — <host>" line. Collapsing on the rendered
  // (label, host) signature keeps every fact's provenance present while
  // removing rows a reader couldn't tell apart — the spec's "no anonymous URL
  // soup". First occurrence wins (the page orders the most prominent facts
  // first). Drop unparseable URLs — a citation with no hostname adds noise, not
  // provenance.
  const seen = new Set<string>();
  const rows: {
    label: string;
    url: string;
    host: string;
    key: string;
    type: SourceType | null;
  }[] = [];
  for (const c of citations) {
    const host = hostname(c.url);
    if (!host) continue;
    const signature = `${c.label} ${host}`;
    if (seen.has(signature)) continue;
    seen.add(signature);
    // The signature is unique among kept rows, so it's a stable React key even
    // when two distinct labels point at the same URL.
    rows.push({
      label: c.label,
      url: c.url,
      host,
      key: signature,
      type: citationSourceType(c.url, {
        companyHost,
        websiteSource,
        websiteSourceUrl,
      }),
    });
  }

  if (rows.length === 0) return null;

  return (
    // id="sources" is the anchor target for the ProvenancePanel's "every figure
    // links to a recorded source" line — keep it in sync with that href.
    <section id="sources" className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Sources</h2>

      <ul className="space-y-2 text-sm">
        {rows.map((row) => (
          <li key={row.key} className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-ink-soft">{row.label}</span>
            <span aria-hidden className="text-ink-faint">
              —
            </span>
            <a
              href={row.url}
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-xs text-ink-muted underline underline-offset-2 hover:text-ink"
            >
              {row.host}
            </a>
            {/* Muted source-type tag — subordinate to the hostname link (no
                underline) but at text-ink-muted so this readable, informational
                label stays legible (the fainter -faint is ~1.4:1 on light).
                Omitted when the type can't be confidently inferred — never a
                guessed attribution. */}
            {row.type && (
              <span className="text-xs text-ink-muted">· {row.type}</span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
