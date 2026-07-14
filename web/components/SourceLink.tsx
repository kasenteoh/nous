// Server component — a very subtle inline "source" affordance rendered next to
// an already-sourced figure on /c/[slug] (total raised, status, website, and
// each funding row). It links out to that figure's recorded source URL, making
// the moat ("every rendered fact is sourced") visible per-fact without
// cluttering the number it sits beside.
//
// Self-omitting by design: renders null when the URL is absent OR does not parse
// as an http(s) URL. The pipeline sometimes stores a scheme-less fallback (e.g.
// the bare-domain `company.website`), and `new URL("acme.com")` throws — such a
// value must NEVER produce a dead superscript. A source affordance that goes
// nowhere would betray the very trust this feature sells, so uncertain → omit.

import type { ReactElement } from "react";

/**
 * The display hostname for an http(s) URL — lowercased, `www.`-stripped — or
 * null when the value is not a parseable http(s) URL. Stricter than a bare
 * `new URL()` (rejects `mailto:` etc.) so the affordance only ever links to a
 * real web source.
 */
function sourceHost(url: string): string | null {
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    const host = u.hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return null;
  }
}

interface Props {
  /** The figure's recorded source URL. Absent/unparseable → the affordance
   *  self-omits (no dead link). */
  url: string | null | undefined;
  /** What the figure is, for the accessible name + hover tooltip (e.g. "Total
   *  raised"). The resolved host is appended so the tooltip reads
   *  "Total raised — source: techcrunch.com". */
  label: string;
}

export function SourceLink({ url, label }: Props): ReactElement | null {
  if (!url) return null;
  const host = sourceHost(url);
  if (!host) return null;

  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title={`${label} — source: ${host}`}
      // Deliberately the faintest muted token + a 10px superscript glyph: it
      // must read as a quiet affordance beside the figure, never compete with
      // it. Brightens one step on hover so it's discoverable without shouting.
      className="ml-0.5 align-super text-[10px] leading-none text-ink-faint no-underline hover:text-ink-muted"
    >
      <span className="sr-only">{`Source for ${label} (${host})`}</span>
      <span aria-hidden>↗</span>
    </a>
  );
}
