// /stats — the public pipeline-freshness page (platform-health observability,
// the "trust signal" half: silent pipeline degradation becomes visible on a
// page instead of being discovered via a stale company profile). Reads the
// pipeline's own audit trail (pipeline_runs — one row per stage execution)
// and reduces it to the latest run per stage. Server-rendered, $0, no new
// pipeline work; degrades to an honest empty state without Supabase.

// Fresher than the content pages' 6h ISR — a freshness page that is itself
// six hours stale would be self-defeating.
export const revalidate = 3600;

import type { Metadata } from "next";
import Link from "next/link";
import { countCompanies, listRecentPipelineRuns } from "@/lib/queries";
import {
  latestActivityAt,
  latestRunsByStage,
  runStatusToneClass,
  stageLabel,
} from "@/lib/stats";
import { formatRelativeTime } from "@/lib/format";

export const metadata: Metadata = {
  // The layout's title template appends " — nous".
  title: "Pipeline status",
  description:
    "When each stage of the nous data pipeline last ran, what it did, and how " +
    "fresh the catalog is — the machinery's pulse, in public.",
  alternates: { canonical: "/stats" },
};

const labelClass =
  "text-[11px] font-medium uppercase tracking-[0.14em] text-ink-muted";

export default async function StatsPage() {
  const [runs, total] = await Promise.all([
    listRecentPipelineRuns(),
    countCompanies(),
  ]);
  const latest = latestRunsByStage(runs);
  const lastActivity = latestActivityAt(runs);

  return (
    <main className="flex-1 px-6 py-12 max-w-4xl mx-auto w-full">
      <header className="mb-10">
        <p className={labelClass}>observability</p>
        <h1 className="mt-3 text-4xl font-semibold tracking-tight text-ink">
          Pipeline status
        </h1>
        <p className="mt-2 text-sm text-ink-muted max-w-2xl leading-relaxed">
          nous refreshes itself around the clock — news every three hours,
          discovery weekly. This page shows the machinery&rsquo;s pulse: the
          latest run of every pipeline stage, straight from the pipeline&rsquo;s
          own audit trail. No hand-curated numbers here either.
        </p>
        {(total > 0 || lastActivity) && (
          <p className="mt-3 text-sm text-ink-muted font-mono">
            {total > 0 && (
              <>{total.toLocaleString("en-US")} companies indexed</>
            )}
            {total > 0 && lastActivity && " · "}
            {lastActivity && (
              <>
                last pipeline activity{" "}
                <span title={lastActivity}>
                  {formatRelativeTime(lastActivity)}
                </span>
              </>
            )}
          </p>
        )}
      </header>

      {latest.length === 0 ? (
        <div className="border border-edge rounded-md px-6 py-10 text-center">
          <p className="text-ink-muted">
            No pipeline telemetry available right now.
          </p>
          <p className="mt-4 text-sm">
            <Link
              href="/companies"
              className="text-accent underline underline-offset-4 decoration-accent/40 hover:decoration-accent transition-colors"
            >
              Browse the catalog →
            </Link>
          </p>
        </div>
      ) : (
        <section aria-label="Latest run per pipeline stage">
          {/* Wide table scrolls inside its own container on small screens. */}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-edge text-left">
                  <th scope="col" className={`${labelClass} py-2 pr-4`}>
                    Stage
                  </th>
                  <th scope="col" className={`${labelClass} py-2 pr-4`}>
                    Last run
                  </th>
                  <th scope="col" className={`${labelClass} py-2 pr-4`}>
                    Status
                  </th>
                  <th
                    scope="col"
                    className={`${labelClass} py-2 text-right`}
                  >
                    Seen → written
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-edge">
                {latest.map((run) => (
                  <tr key={run.stage}>
                    <td className="py-2.5 pr-4 text-ink">
                      {stageLabel(run.stage)}
                    </td>
                    <td
                      className="py-2.5 pr-4 font-mono text-xs text-ink-muted whitespace-nowrap"
                      title={run.finished_at}
                    >
                      {formatRelativeTime(run.finished_at)}
                    </td>
                    <td className="py-2.5 pr-4">
                      <span
                        className={`font-mono text-xs ${runStatusToneClass(run.status)}`}
                      >
                        {run.status}
                      </span>
                    </td>
                    <td className="py-2.5 font-mono text-xs text-ink-muted text-right whitespace-nowrap">
                      {run.inputs_seen.toLocaleString("en-US")} →{" "}
                      {run.rows_written.toLocaleString("en-US")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs text-ink-muted max-w-2xl leading-relaxed">
            Status vocabulary: <span className="font-mono">success</span> — the
            stage ran and wrote what it saw;{" "}
            <span className="font-mono">empty</span> — it processed inputs but
            wrote nothing (often just a quiet cycle);{" "}
            <span className="font-mono">error</span> — it failed and will retry
            on the next run. Stages outside the recent window are omitted
            rather than guessed at.
          </p>
        </section>
      )}
    </main>
  );
}
