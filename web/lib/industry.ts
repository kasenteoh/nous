// Pure slug↔label mapping for the industry_group landing pages. `industry_group`
// values are LLM-emitted freeform labels ("AI Infrastructure", "Fintech /
// Payments", …); this module derives a stable URL slug from a label and
// resolves a slug back to its canonical label against a supplied bucket list.
// No DB access — the canonical list comes from the query layer
// (`listCanonicalIndustries`) and is passed in, so this stays trivially
// testable and import-safe anywhere (no `server-only`).

/**
 * Kebab-case a label into a URL-safe slug: lowercased, every run of
 * non-alphanumerics collapsed to a single hyphen, no leading/trailing hyphen.
 * "AI / ML Infrastructure" → "ai-ml-infrastructure".
 */
export function industryToSlug(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

/**
 * Resolve a URL slug to its canonical `industry_group` label, or null if the
 * slug matches no canonical bucket. `canonical` is the gated bucket list
 * (`listCanonicalIndustries` — groups applying to ≥ the min company count);
 * resolving ONLY against it is the hard gate that stops `/industry/[group]`
 * from rendering an arbitrary freeform label as a page. On the vanishingly
 * rare chance two labels slugify identically, the first in the caller-sorted
 * list wins deterministically.
 */
export function resolveIndustrySlug(
  slug: string,
  canonical: readonly string[],
): string | null {
  for (const label of canonical) {
    if (industryToSlug(label) === slug) return label;
  }
  return null;
}
