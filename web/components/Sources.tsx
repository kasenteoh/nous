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

interface Props {
  /** Citations in display order. The component de-dupes identical URLs (first
   *  label wins) and drops any whose URL is unparseable. */
  citations: Citation[];
}

export function Sources({ citations }: Props) {
  // De-dupe on URL: the same article can source several facts, but it should
  // appear once. First occurrence wins its label (the page orders the most
  // specific facts first). Drop unparseable URLs — a citation we can't render a
  // hostname for adds noise, not provenance.
  const seen = new Set<string>();
  const rows: { label: string; url: string; host: string }[] = [];
  for (const c of citations) {
    if (seen.has(c.url)) continue;
    const host = hostname(c.url);
    if (!host) continue;
    seen.add(c.url);
    rows.push({ label: c.label, url: c.url, host });
  }

  if (rows.length === 0) return null;

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-ink mb-4">Sources</h2>

      <ul className="space-y-2 text-sm">
        {rows.map((row) => (
          <li key={row.url} className="flex flex-wrap items-baseline gap-x-2">
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
          </li>
        ))}
      </ul>
    </section>
  );
}
