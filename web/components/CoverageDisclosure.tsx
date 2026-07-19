// Server component — the shared "Covered by …" disclosure. Used by BOTH
// FundingTimeline (a round's press coverage) and NewsSection (a story's
// syndicated copies), so one implementation defines the collapsed-coverage
// behavior everywhere (split out of the old EventTimeline).

import type { CoverageLink } from "@/lib/timeline";

/**
 * Press coverage, collapsed. Native <details> (server-component-safe,
 * keyboard-operable, exposes open state to assistive tech for free): the summary
 * names the first two hosts + a "+N more sources" count; expanding lists every
 * article as a source link. Trust-preserving — every source is one click away,
 * never dropped.
 */
export function CoverageDisclosure({ coverage }: { coverage: CoverageLink[] }) {
  // Name DISTINCT outlets (two URLs from one outlet must not read "Covered by
  // techcrunch.com, techcrunch.com"); the count is remaining distinct outlets.
  const outlets = [...new Set(coverage.map((c) => c.host))];
  const shown = outlets.slice(0, 2);
  const extra = outlets.length - shown.length;
  return (
    <details className="group mt-1.5">
      <summary className="flex w-fit cursor-pointer list-none items-center gap-1.5 text-sm text-ink-muted hover:text-ink [&::-webkit-details-marker]:hidden">
        <svg
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          aria-hidden
          className="h-3 w-3 shrink-0 text-ink-faint transition-transform group-open:rotate-90"
        >
          <path d="M7 4l7 6-7 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <span>
          Covered by {shown.join(", ")}
          {extra > 0 && (
            <span className="text-ink-muted">
              {" "}
              +{extra} more source{extra === 1 ? "" : "s"}
            </span>
          )}
        </span>
      </summary>
      <ul className="mt-2 ml-[18px] flex flex-col gap-1.5">
        {coverage.map((c) => (
          <li key={c.url} className="text-sm leading-snug">
            <a
              href={c.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-ink-muted underline-offset-2 hover:text-ink hover:underline"
            >
              {c.title ?? c.host}
            </a>
            {c.title && (
              <span className="ml-1.5 text-xs text-ink-muted">· {c.host}</span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}
