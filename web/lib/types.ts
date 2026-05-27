// Row types matching the M1 DB schema defined in pipeline/src/nous/db/models.py.
// Column names must match exactly — Supabase returns them as-is.

export interface CompanyRow {
  id: string;
  cik: string | null;
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
  // One of: 'form_d' | 'vc_portfolio' | 'news' | 'techcrunch'.
  discovered_via: string;
  created_at: string;
  updated_at: string;
}

export interface FilingRow {
  id: string;
  company_id: string;
  accession_number: string;
  filing_date: string; // ISO date string (YYYY-MM-DD)
  offering_amount_total: number | null;
  amount_sold: number | null;
  investors_count: number | null;
  minimum_investment: number | null;
  raw_data: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface RelatedPersonRow {
  id: string;
  company_id: string;
  filing_id: string;
  name: string;
  relationship: string;
  address: {
    street?: string;
    city?: string;
    state?: string;
    zip?: string;
    country?: string;
  } | null;
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
  latest_filing_date: string | null; // ISO date
  latest_offering_amount: number | null;
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
  filing_id: string | null;
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

/** Full company detail assembled from three DB queries. */
export interface CompanyDetail {
  company: CompanyRow;
  filings: FilingRow[]; // sorted by filing_date desc
  relatedPersons: RelatedPersonRow[]; // most recent filing's people first
  fundingRounds: FundingRoundWithInvestors[]; // sorted by announced_date desc (nulls last)
}
