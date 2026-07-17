// Pure helpers for the /stats pipeline-freshness page. No DB, no React —
// unit-testable reductions over pipeline_runs rows (the pipeline's own
// observability audit trail: one row per stage execution with status
// 'success' | 'empty' | 'error' and input/output counts).

import type { PipelineRunRow } from "@/lib/types";

/** Human labels for the stage ids the pipeline records. Unknown stages fall
 *  back to their raw id — the page must render new stages without a deploy. */
export const STAGE_LABELS: Record<string, string> = {
  "ingest-news": "News ingestion",
  "extract-funding": "Funding extraction",
  "extract-funding-website": "Funding gap-fill (company sites)",
  "backfill-funding-history": "Funding history backfill",
  "refresh-latest-round": "Latest-round refresh",
  "resolve-website-fallback": "Website re-mining (husks)",
  "resolve-homepages": "Homepage resolution",
  "scrape-homepages": "Homepage scraping",
  "enrich-companies": "LLM enrichment",
  "embed-companies": "Embeddings",
  "judge-eligibility": "Eligibility judging",
  "verify-sources": "Source verification",
  "repair-catalog": "Catalog repair",
  "repair-wrong-websites": "Wrong-website repair",
  "repair-duplicate-rounds": "Duplicate-round repair",
  "normalize-taxonomy": "Taxonomy normalization",
  "normalize-hq-state": "HQ-state normalization",
  "refresh-vc-portfolios": "VC portfolio refresh",
  "discover-github-trending": "GitHub-trending discovery",
  "dedup-companies": "Company dedup",
  "dedup-investors": "Investor dedup",
  "analyze-competitors": "Competitor analysis",
  "estimate-employees": "Employee estimation",
  "snapshot-companies": "Weekly snapshots",
  "compute-themes": "Theme clustering",
  "compute-map-positions": "Market-map coordinates",
  "compute-momentum": "Momentum scores",
  "compute-completeness": "Completeness scores",
  "extract-career-history": "Founder-background extraction",
  "repair-misattributed-news": "Misattributed-news repair",
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

/**
 * Reduce a newest-first run list to the LATEST run per stage, preserving
 * newest-first order across stages. The query hands us a bounded recent
 * window, so a stage that hasn't run inside it simply doesn't appear —
 * honest omission, not a fabricated "never ran".
 */
export function latestRunsByStage(
  runs: readonly PipelineRunRow[],
): PipelineRunRow[] {
  const seen = new Set<string>();
  const latest: PipelineRunRow[] = [];
  for (const run of runs) {
    if (seen.has(run.stage)) continue;
    seen.add(run.stage);
    latest.push(run);
  }
  return latest;
}

/** The most recent finished_at across all runs, or null when there are none. */
export function latestActivityAt(
  runs: readonly PipelineRunRow[],
): string | null {
  return runs.length > 0 ? runs[0].finished_at : null;
}

/** Tone class for a run status: success stays quiet, empty/error warn. */
export function runStatusToneClass(status: string): string {
  return status === "success" ? "text-money" : "text-warn";
}
