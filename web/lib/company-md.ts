// Pure markdown rendering for the /c/[slug].md AI-answer surface (ROADMAP
// Later #2). Answer engines increasingly read markdown siblings of pages
// (the llms.txt convention), and nous's fully-sourced data is exactly what
// they reward — so every fact here carries its recorded source URL inline,
// and facts the pipeline has discriminatively verified (fact_verifications,
// `supported` + grounded quote) are annotated as verified.
//
// Same trust rules as the HTML page: unknown values are OMITTED, never
// guessed; competitor entries pass the same meta-leak guard; the displayed
// total and its citation come from the same computeTotalRaised invariant the
// tile uses, so the two surfaces can never disagree. Pure (no DB, no React)
// so it is unit-testable.

import type { CompanyDetail, FundingRoundWithInvestors } from "@/lib/types";
import { computeTotalRaised } from "@/lib/funding";
import { formatDate, formatUsd } from "@/lib/format";
import { competitorLeaksMeta } from "@/lib/competitor-guards";
import {
  buildVerificationLookup,
  verifiedAgainst,
} from "@/lib/verifications";

const VERIFIED_NOTE = "✓ verified against the cited source";

/** "$40.0M — Series B, announced 2026-03-01" style one-liner for a round. */
function roundLine(
  round: FundingRoundWithInvestors,
  verified: boolean,
): string {
  const parts: string[] = [];
  const amount =
    round.amount_raised != null ? formatUsd(round.amount_raised) : null;
  parts.push(
    `**${round.round_type ?? "Funding round"}**${amount ? ` — ${amount}` : ""}`,
  );
  if (round.valuation_post_money != null) {
    parts.push(`at ${formatUsd(round.valuation_post_money)} post-money`);
  }
  if (round.announced_date) {
    parts.push(`(announced ${formatDate(round.announced_date)})`);
  }
  const investors: string[] = [
    ...round.leadInvestors.map((n) => `${n} (lead)`),
    ...round.otherInvestors,
  ];
  let line = `- ${parts.join(" ")}`;
  if (investors.length > 0) line += ` — investors: ${investors.join(", ")}`;
  if (round.primary_news_url) line += ` — source: ${round.primary_news_url}`;
  if (verified) line += ` — ${VERIFIED_NOTE}`;
  return line;
}

/**
 * Render one company as a self-contained markdown document. `origin` is the
 * canonical site origin (no trailing slash).
 */
export function renderCompanyMarkdown(
  detail: CompanyDetail,
  origin: string,
): string {
  const { company, people, fundingRounds, competitors, news, verifications } =
    detail;
  const vlookup = buildVerificationLookup(verifications);
  const lines: string[] = [];

  lines.push(`# ${company.name}`);
  lines.push("");
  // The blockquote lead is the company's one-liner — but ONLY when it was
  // written from the company's OWN website. A describe-fallback description
  // (description_source === "fallback", migration 0045) is grounded in
  // third-party evidence (Wikidata + press), and this .md sibling is a
  // machine-consumed surface with no per-fact attribution slot, so a
  // third-party-grounded one-liner must never lead it unattributed. Revisit
  // with an inline attribution note as a follow-up.
  if (company.description_short && company.description_source !== "fallback") {
    lines.push(`> ${company.description_short}`);
    lines.push("");
  }

  // ── Key facts (omit-when-unknown; per-fact sources inline) ────────────────
  const facts: string[] = [];
  if (company.website) facts.push(`- Website: ${company.website}`);
  const hq = [company.hq_city, company.hq_state, company.hq_country]
    .filter(Boolean)
    .join(", ");
  if (hq) facts.push(`- Headquarters: ${hq}`);
  if (company.year_incorporated) {
    facts.push(`- Founded: ${company.year_incorporated}`);
  }
  if (company.industry_group) {
    facts.push(`- Industry: ${company.industry_group}`);
  }
  if (company.tags && company.tags.length > 0) {
    facts.push(`- Tags: ${company.tags.join(", ")}`);
  }
  if (
    company.employee_count_min != null ||
    company.employee_count_max != null
  ) {
    const lo = company.employee_count_min ?? "?";
    const hi = company.employee_count_max ?? "?";
    facts.push(`- Employees: ${lo}–${hi} (estimated)`);
  }
  if (company.status !== "active") {
    let statusLine = `- Status: ${company.status.replace("_", " ")}`;
    if (company.status_source_url) {
      statusLine += ` — source: ${company.status_source_url}`;
      if (
        verifiedAgainst(vlookup, "status", "", company.status_source_url, {
          kind: "status",
          status: company.status,
        })
      ) {
        statusLine += ` — ${VERIFIED_NOTE}`;
      }
    }
    facts.push(statusLine);
  }

  // The displayed total and its citation follow the SAME invariant as the
  // page tile (max of stated vs deduped round sum; stated figure cites its
  // article, a summed figure has no single citation here).
  const { total, statedWins, hasTotal } = computeTotalRaised(
    company.total_raised_usd ?? null,
    fundingRounds,
  );
  if (hasTotal && total > 0) {
    let totalLine = `- Total raised: ${formatUsd(total)}`;
    if (statedWins && company.total_raised_as_of) {
      totalLine += ` (as of ${formatDate(company.total_raised_as_of)})`;
    }
    if (statedWins && company.total_raised_source_url) {
      totalLine += ` — source: ${company.total_raised_source_url}`;
      if (
        verifiedAgainst(
          vlookup,
          "total_raised",
          "",
          company.total_raised_source_url,
          { kind: "amount", amountUsd: total },
        )
      ) {
        totalLine += ` — ${VERIFIED_NOTE}`;
      }
    }
    facts.push(totalLine);
  }
  if (facts.length > 0) {
    lines.push(...facts);
    lines.push("");
  }

  if (fundingRounds.length > 0) {
    lines.push("## Funding rounds");
    lines.push("");
    for (const round of fundingRounds) {
      const verified =
        round.primary_news_url != null &&
        verifiedAgainst(
          vlookup,
          "funding_round",
          round.id,
          round.primary_news_url,
          { kind: "amount", amountUsd: round.amount_raised },
        ) !== null;
      lines.push(roundLine(round, verified));
    }
    lines.push("");
  }

  if (people.length > 0) {
    lines.push("## Leadership");
    lines.push("");
    for (const person of people) {
      lines.push(`- ${person.name} — ${person.role}`);
    }
    lines.push("");
  }

  // Same display guard as the HTML page: entries whose description/reasoning
  // leak LLM scratch notes are dropped, and the section is labeled AI-inferred.
  const cleanCompetitors = competitors.filter((c) => !competitorLeaksMeta(c));
  if (cleanCompetitors.length > 0) {
    lines.push("## Competitors (AI-inferred)");
    lines.push("");
    for (const competitor of cleanCompetitors.slice(0, 8)) {
      lines.push(`- ${competitor.competitor_name}`);
    }
    lines.push("");
  }

  if (company.description_long) {
    lines.push("## About");
    lines.push("");
    lines.push(company.description_long);
    lines.push("");
    // A describe-fallback profile (description_source === "fallback", migration
    // 0045 / prompt 2026-07-19.2) is written from third-party evidence, not the
    // company's own site. Unlike the short blockquote lead — which this
    // machine-consumed surface GATES out entirely, having no attribution slot
    // for a bare one-liner — the About section is a document with room for an
    // inline attribution line, so a fallback long profile is syndicated WITH its
    // provenance rather than withheld. An own-website long is unchanged.
    if (company.description_source === "fallback") {
      lines.push("*Profile written by nous from Wikidata and press coverage.*");
      lines.push("");
    }
  }

  if (news.length > 0) {
    lines.push("## Recent coverage");
    lines.push("");
    for (const article of news.slice(0, 10)) {
      const date = article.published_date
        ? ` (${formatDate(article.published_date)})`
        : "";
      lines.push(`- [${article.title}](${article.url})${date}`);
    }
    lines.push("");
  }

  lines.push("---");
  lines.push("");
  lines.push(
    `Compiled by nous from public sources — every figure above carries its ` +
      `recorded source, and facts carrying a verification mark passed a ` +
      `discriminative check against that source's stored text. Unknown ` +
      `values are omitted, never guessed.`,
  );
  lines.push("");
  lines.push(`- Web page: ${origin}/c/${company.slug}`);
  lines.push(`- Feed: ${origin}/c/${company.slug}/feed.xml`);
  lines.push(`- Site guide for language models: ${origin}/llms.txt`);
  lines.push("");
  return lines.join("\n");
}
