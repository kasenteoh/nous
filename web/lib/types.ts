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
  latest_filing_date: string | null; // ISO date
  latest_offering_amount: number | null;
}

/** Full company detail assembled from three DB queries. */
export interface CompanyDetail {
  company: CompanyRow;
  filings: FilingRow[]; // sorted by filing_date desc
  relatedPersons: RelatedPersonRow[]; // most recent filing's people first
}
