// Row types matching the M1 DB schema defined in pipeline/src/nous/db/models.py.
// Column names must match exactly — Supabase returns them as-is.

export interface CompanyRow {
  id: string;
  name: string;
  slug: string;
  normalized_name: string;
  description_short: string | null; // filled in M2
  description_long: string | null; // filled in M2
  primary_category: string | null; // filled in M2
  tags: string[] | null; // filled in M2 — Supabase returns Postgres text[] as string[]
  website: string | null; // filled in M2
  logo_url: string | null; // filled in M2
  hq_city: string | null;
  hq_state: string | null;
  hq_country: string | null;
  year_incorporated: number | null;
  industry_group: string | null;
  employee_count_min: number | null; // filled later
  employee_count_max: number | null; // filled later
  employee_count_source: string | null;
  last_enriched_at: string | null;
  // M3 — how this company first entered the DB.
  // One of: 'vc_portfolio' | 'news' | 'techcrunch'.
  discovered_via: string;
  // Lifecycle status — 'active' | 'acquired' | 'shut_down' | 'ipo'. Set by the
  // extract-funding stage from explicit announcements; defaults to 'active'.
  status: string;
  // Article/page that announced the status event; null while status='active'.
  status_source_url: string | null;
  // Consecutive homepage-fetch failures across scrape-homepages runs. The
  // scraper bumps this on a total fetch failure, resets it on success, and
  // leaves it unchanged on a robots.txt block. A high value is a low-confidence
  // "possibly inactive" signal, surfaced as muted text (not a badge) on the
  // detail page. See INACTIVE_FAILURE_THRESHOLD.
  consecutive_scrape_failures: number;
  created_at: string;
  updated_at: string;
}

// ─── Derived / query result types ─────────────────────────────────────────────

/** Projection used by the company index page listing. */
export interface CompanyListRow {
  slug: string;
  name: string;
  hq_city: string | null;
  hq_state: string | null;
  industry_group: string | null;
  description_short: string | null; // M2 — shown as preview on index cards
  status: string; // 'active' | 'acquired' | 'shut_down' | 'ipo'
}

// ─── M3: funding-history rows ─────────────────────────────────────────────────

/**
 * Row from the `investors` table. `name_normalized` is the lowercased form used
 * for unique-on-rename matching server-side; the UI always shows `name`.
 */
export interface Investor {
  id: string;
  name: string;
  name_normalized: string;
  type: string; // 'institutional' | 'angel' | 'unknown'
  description: string | null;
  website: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Row from `funding_rounds`. Numeric columns come back from Supabase as
 * strings (Postgres `numeric` is not safely coercible to JS `number`), but the
 * JS client serializes them as `number` for us — so we type them as `number`
 * here. If we ever see precision loss, we'll switch to `string`.
 */
export interface FundingRound {
  id: string;
  company_id: string;
  round_type: string | null;
  amount_raised: number | null;
  valuation_post_money: number | null;
  valuation_source: string | null;
  announced_date: string | null; // ISO date (YYYY-MM-DD) or null
  primary_news_url: string | null;
  extraction_confidence: string | null; // 'low' | 'medium' | 'high' | null
  created_at: string;
  updated_at: string;
}

/**
 * A funding round joined with its investor names, pre-split into lead vs
 * other participants. Built in `getCompanyBySlug` from the nested-select.
 */
export interface FundingRoundWithInvestors extends FundingRound {
  leadInvestors: string[];
  otherInvestors: string[];
}

// ─── M4: competitors ──────────────────────────────────────────────────────────

/**
 * Row from the `competitors` table. `competitor_company_id` is non-null when
 * the LLM-named competitor resolves to an indexed company via exact
 * normalized_name match; otherwise the competitor is stored text-only.
 */
export interface CompetitorRow {
  id: string;
  company_id: string;
  competitor_company_id: string | null;
  competitor_name: string;
  description: string | null;
  reasoning: string | null;
  rank: number;
  // 'techcrunch' (named in the company's TechCrunch coverage) | 'llm_inferred'
  // (general-knowledge competitor, shown as "potential").
  source: string;
  source_url: string | null; // the TechCrunch article when source='techcrunch'
  created_at: string;
  updated_at: string;
}

/**
 * A competitor joined with the resolved company's slug + name, when present.
 * Built in `getCompanyBySlug` from the nested-select.
 */
export interface CompetitorWithResolved extends CompetitorRow {
  resolved: { slug: string; name: string } | null;
}

/** Row from the `people` table — website-sourced leadership/founders. */
export interface PersonRow {
  id: string;
  company_id: string;
  name: string;
  role: string;
  source_url: string | null;
  rank: number;
  created_at: string;
  updated_at: string;
}

/** Company-level investor (VC firm), shaped from the company_investors join. */
export interface CompanyInvestorRow {
  name: string;
  website: string | null;
  isLead: boolean;
  source: string;
}

/** Row from the `news_articles` table, for the News section. */
export interface NewsArticleRow {
  id: string;
  url: string;
  title: string;
  source: string;
  published_date: string | null;
}

/** Full company detail assembled from the DB queries. */
export interface CompanyDetail {
  company: CompanyRow;
  people: PersonRow[]; // ordered by rank ascending
  fundingRounds: FundingRoundWithInvestors[]; // sorted by announced_date desc (nulls last)
  competitors: CompetitorWithResolved[]; // sorted by rank ascending
  investors: CompanyInvestorRow[]; // company-level investors (VC firms)
  news: NewsArticleRow[]; // recent news articles, newest first
}
