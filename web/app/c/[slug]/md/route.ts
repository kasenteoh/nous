// /c/[slug].md — the markdown sibling of the company page (llms.txt
// convention), served for AI answer engines. The public URL is /c/<slug>.md;
// a next.config rewrite maps it here (a dynamic segment can't carry a literal
// ".md" suffix). Same data as the HTML page via getCompanyBySlug; rendering
// is the pure lib/company-md.ts (per-fact sources inline, verified facts
// annotated, unknowns omitted).

import { permanentRedirect } from "next/navigation";
import { getAliasTargetSlug, getCompanyBySlug } from "@/lib/queries";
import { renderCompanyMarkdown } from "@/lib/company-md";
import { siteOrigin } from "@/lib/site";

// Regenerate at most every 6 hours, matching the pages' ISR window.
export const revalidate = 21600;

type RouteContext = { params: Promise<{ slug: string }> };

export async function GET(
  _req: Request,
  { params }: RouteContext,
): Promise<Response> {
  const { slug } = await params;
  const detail = await getCompanyBySlug(slug);

  if (!detail) {
    // Renamed/merged slug → the canonical markdown URL (mirrors the HTML
    // page's alias handling); truly unknown → a plain-text 404.
    const target = await getAliasTargetSlug(slug);
    if (target) permanentRedirect(`/c/${target}.md`);
    return new Response("Not found.\n", {
      status: 404,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }

  return new Response(renderCompanyMarkdown(detail, siteOrigin()), {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "public, max-age=3600",
    },
  });
}
