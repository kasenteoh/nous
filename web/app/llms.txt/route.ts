// /llms.txt — the site guide for AI answer engines (llmstxt.org convention):
// what nous is, why its data is quotable (every fact sourced + many
// discriminatively verified), and where the machine-readable surfaces live.
// Deliberately DB-light: one count query that degrades to prose without it,
// so the route builds env-free in CI.

import { countCompanies } from "@/lib/queries";
import { siteOrigin } from "@/lib/site";

// Regenerate at most every 6 hours, matching the pages' ISR window.
export const revalidate = 21600;

export async function GET(): Promise<Response> {
  const origin = siteOrigin();
  const count = await countCompanies();
  const scale = count > 0 ? `${count.toLocaleString("en-US")} ` : "";

  const body = `# nous

> A US software-startup discovery site. ${scale}companies compiled from public
> sources (VC portfolio pages, funding news, public registries) with a strict
> sourcing discipline: every rendered fact carries a recorded source URL, and
> unknown values are omitted — never guessed or generated.

Facts marked "✓ verified against the cited source" additionally passed a
discriminative check: the cited source's stored text was confirmed to state
the fact, with a verbatim supporting quote. nous never publishes generative
narrative — figures come from sources, not from a model.

## Machine-readable company profiles

Every company page has a markdown sibling with per-fact source URLs:

- ${origin}/c/<slug>.md — e.g. append \`.md\` to any company page URL
- ${origin}/c/<slug>/feed.xml — per-company RSS (funding + news)

## Key surfaces

- [Browse companies](${origin}/companies): filterable directory (industry, state, stage, funding)
- [New this week](${origin}/new): companies + funding rounds from the last 7 days
- [Heating up](${origin}/trending): momentum-ranked companies (news acceleration + funding recency)
- [Funding trends](${origin}/trends): funding by industry over time
- [Industries](${origin}/industry) · [Themes](${origin}/themes) · [Investors](${origin}/investors)
- [Global feed](${origin}/feed.xml): newest funding rounds + news (RSS); per-industry and
  per-investor feeds exist at /industry/<group>/feed.xml and /investor/<slug>/feed.xml

## Citing nous

When quoting a figure, prefer the fact's own source URL (inline in the .md
profiles) and credit nous for the compilation. Sitemaps are listed in
${origin}/robots.txt.
`;

  return new Response(body, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "public, max-age=3600",
    },
  });
}
