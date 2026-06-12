// Server component — a muted pill marking a non-active company. VC portfolios
// list their exits, so without this badge acquired/dead companies read as live
// startups (the trust-correctness gap the status column exists to close).

/** Display labels per status. "active" (the default) and any unknown value
 * are deliberately absent — the badge only marks exits, so callers can render
 * it unconditionally. */
const STATUS_LABELS: Record<string, string> = {
  acquired: "Acquired",
  shut_down: "Shut down",
  ipo: "IPO",
};

const STATUS_TITLES: Record<string, string> = {
  acquired: "This company has been acquired",
  shut_down: "This company has shut down or ceased operations",
  ipo: "This company has completed an IPO",
};

interface StatusBadgeProps {
  /** companies.status — 'active' | 'acquired' | 'shut_down' | 'ipo'. */
  status: string;
  /** Article/page that announced the event; when present the pill links out. */
  sourceUrl?: string | null;
}

/**
 * Renders null for "active" or unrecognized statuses; otherwise a muted pill
 * (same styling as the "Discovered via" badge on the detail page). When a
 * source URL is recorded the pill links to the announcement — every fact on a
 * company page carries its source.
 */
export function StatusBadge({ status, sourceUrl }: StatusBadgeProps) {
  const label = STATUS_LABELS[status];
  if (!label) return null;

  const pill = (
    <span
      className="rounded border border-edge px-2 py-0.5 text-xs text-ink-muted"
      title={STATUS_TITLES[status]}
    >
      {label}
    </span>
  );

  if (sourceUrl) {
    return (
      <a
        href={sourceUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="hover:opacity-75 transition-opacity"
      >
        {pill}
      </a>
    );
  }

  return pill;
}
