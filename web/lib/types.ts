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
  // Hybrid "total raised" (migration 0021): an article-STATED cumulative
  // total ("has raised $285M to date"), distinct from the sum of
  // funding_rounds — news discovery never backfills historical rounds, so the
  // sum undercounts older companies. Optional (`?`), not just nullable: prod
  // rows lack these columns until the migration runs there, and select("*")
  // simply omits unknown columns, so the keys may be absent at runtime.
  // Treat undefined as null. The three fields always travel together.
  total_raised_usd?: number | null;
  total_raised_source_url?: string | null;
  total_raised_as_of?: string | null; // ISO date (YYYY-MM-DD) or null
  // Consecutive homepage-fetch failures across scrape-homepages runs. The
  // scraper bumps this on a total fetch failure, resets it on success, and
  // leaves it unchanged on a robots.txt block. A high value is a low-confidence
  // "possibly inactive" signal, surfaced as muted text (not a badge) on the
  // detail page. See INACTIVE_FAILURE_THRESHOLD.
  consecutive_scrape_failures: number;
  // Catalog-quality soft exclusion (migration 0022). NULL/undefined = included.
  // Optional (`?`), not just nullable: prod rows lack the column until the
  // migration runs there; select("*") omits unknown columns. Treat undefined
  // as null. A non-null value means the company page must 404.
  exclusion_reason?: string | null;
  // Denormalized count(funding_rounds) (migration 0022) backing the catalog
  // bar. Same optionality caveat as above.
  funding_round_count?: number | null;
  // Denormalized most-recent funding round (migration 0028), maintained by the
  // refresh-latest-round stage; backs the browse-page funding/recency sorts and
  // the stage / funded-since filters. Optional for the same reason as the
  // total_raised_* fields above: prod rows lack these columns until the
  // migration runs there, and select("*") omits unknown columns.
  latest_round_amount?: number | null;
  latest_round_date?: string | null; // ISO date (YYYY-MM-DD) or null
  latest_round_type?: string | null;
  // Momentum / "heating up" signal (migration 0039), pipeline-computed. Score is
  // in [0,1] (0.5 = flat, higher = accelerating), NULL until a company has
  // enough history to score. `momentum_why` is a pre-worded breakdown
  // (["+40% team", "5 news mentions"]) the web joins verbatim. Optional (`?`),
  // not just nullable — same reason as total_raised_* / latest_round_*: prod
  // rows lack these columns until the migration runs there, and select("*")
  // omits unknown columns, so the keys may be absent at runtime. The detail
  // page's MomentumBadge reads momentum_score and degrades to no badge when the
  // column is absent (undefined → isHeatingUp false).
  momentum_score?: number | null;
  momentum_computed_at?: string | null; // ISO timestamp or null
  momentum_why?: string[] | null; // pre-worded breakdown; Postgres text[]
  // Completeness / "documented" signal (migration 0042), pipeline-computed by the
  // compute-completeness stage from util.completeness (the SOLE scorer — the web
  // never re-derives it). Score is in [0,1] (share of key profile fields filled
  // in), NULL until the stage runs for a company. The detail page's
  // ProvenancePanel reads completeness_score to show a positive-only "documented"
  // badge (gated ≥0.5) and degrades to no badge when the column is absent
  // (undefined → below every threshold). Optional (`?`), not just nullable —
  // same reason as momentum_* / total_raised_* / latest_round_* / the *_checked_at
  // freshness stamps below: prod rows lack these columns until the migration runs
  // there, and select("*") omits unknown columns, so the keys may be absent at
  // runtime. Treat undefined as null.
  completeness_score?: number | null;
  completeness_computed_at?: string | null; // ISO timestamp or null
  // Per-enrichment-stage freshness stamps (each set when that stage last touched
  // the company). The ProvenancePanel derives "Last verified N days ago" from the
  // MAX of these plus last_enriched_at, computed read-time (no dedicated column).
  // Same optionality caveat as above: absent on pre-migration prod rows, so the
  // panel simply omits the line when none is present rather than fabricating one.
  website_resolved_at?: string | null;
  website_fallback_checked_at?: string | null;
  news_checked_at?: string | null;
  website_funding_checked_at?: string | null;
  employee_count_checked_at?: string | null;
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
  // External favicon URL backfilled by the pipeline; null/absent until then.
  // Carried so CompanyCard renders the real logo instead of the monogram
  // fallback. Optional (`?`): some projections (e.g. the investor-portfolio
  // join) don't select it — those rows simply fall back to the monogram.
  logo_url?: string | null;
}

/**
 * A "heating up" company for /trending: the CompanyCard projection plus the
 * pipeline-computed momentum fields. `momentumScore` is in [0,1] (0.5 = flat,
 * higher = accelerating). `momentumWhy` is a pre-worded breakdown
 * (["+40% team", "5 news mentions", "raised 3wks ago"]) rendered join(" · "),
 * mirroring Spotlight.facts — the web never computes it. All momentum columns
 * land via migration 0039; until then the query 400s → [] (see
 * {@link listHeatingUpCompanies}), so the page shows its empty state.
 */
export interface MomentumCompany extends CompanyListRow {
  momentumScore: number;
  momentumComputedAt: string | null; // ISO timestamp or null
  momentumWhy: string[];
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

// ─── "Alternatives to X" pages (SEO) ──────────────────────────────────────────

/**
 * A competitor that resolved to an indexed company, shaped for /alternatives/
 * [slug]. Extends the {@link CompanyListRow} card projection (so it renders in
 * a CompanyCard, with its logo) with the competitor-edge context — why nous
 * lists it as an alternative and where that came from.
 */
export interface AlternativeCompany extends CompanyListRow {
  /** Competitor `rank` (1 = most relevant); orders the list. */
  rank: number;
  /** LLM rationale for why the two compete, or null. */
  reasoning: string | null;
  /** Short competitor description, or null. */
  description: string | null;
  /** 'techcrunch' | 'llm_inferred' — provenance of the competitor edge. */
  source: string;
  /** The TechCrunch article when source='techcrunch', else null. */
  source_url: string | null;
}

/**
 * An LLM-named competitor that did NOT resolve to an indexed company — there's
 * no /c/[slug] for it, so it renders as a plain name + reasoning rather than a
 * linked card.
 */
export interface NamedAlternative {
  name: string;
  rank: number;
  reasoning: string | null;
  description: string | null;
  source: string;
  source_url: string | null;
}

/**
 * Everything the /alternatives/[slug] page needs: the subject company's
 * display fields plus its competitors, split into ones resolved to indexed
 * companies (linked cards) and LLM-named ones (text only). Built by
 * {@link getAlternatives}; null when the slug is unknown/excluded.
 */
export interface AlternativesData {
  company: {
    slug: string;
    name: string;
    description_short: string | null;
    industry_group: string | null;
  };
  resolved: AlternativeCompany[];
  named: NamedAlternative[];
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

/**
 * One founder/exec and one prior employer, shaped from the career_moves join
 * (the talent-flow "founder background" rider). `priorCompanySlug` is set only
 * when the verbatim `priorCompanyName` resolves to a SHOWN catalog company
 * (else the name renders as plain text with no link).
 */
export interface CareerMove {
  personName: string;
  priorCompanyName: string;
  priorRole: string | null;
  startYear: number | null;
  endYear: number | null;
  priorCompanySlug: string | null;
}

/** Company-level investor (VC firm), shaped from the company_investors join. */
export interface CompanyInvestorRow {
  name: string;
  website: string | null;
  isLead: boolean;
  source: string;
}

// ─── Investor pages ───────────────────────────────────────────────────────────

/** One row on the /investors index: a firm plus its portfolio company count. */
export interface InvestorListRow {
  slug: string;
  name: string;
  type: string; // 'institutional' | 'angel' | 'unknown'
  portfolioCount: number;
}

/**
 * One funding round an investor participated in, flattened with the company it
 * funded, for the investor detail page's activity/rounds sections.
 */
export interface InvestorRoundRow {
  roundId: string;
  isLead: boolean;
  round_type: string | null;
  amount_raised: number | null;
  announced_date: string | null; // ISO date or null
  primary_news_url: string | null;
  companySlug: string;
  companyName: string;
}

/** Full detail for a single investor, assembled in getInvestorBySlug. */
export interface InvestorDetail {
  slug: string;
  name: string;
  type: string;
  description: string | null;
  website: string | null;
  /**
   * Denormalized total from `investors.portfolio_count` (migration 0025) —
   * counts DISTINCT non-excluded companies via EITHER company_investors OR
   * funding_round_investors. Matches the /investors index. Falls back to
   * portfolio.length when the column is not yet populated in prod.
   */
  portfolioCount: number;
  /**
   * Portfolio companies for the requested page (company_investors join unioned
   * with round-only companies, shaped for CompanyCard). When getInvestorBySlug
   * is called with a `limit`, this holds only that page's slice; without one it
   * is the full union.
   */
  portfolio: CompanyListRow[];
  /**
   * Length of the full, deduplicated portfolio union (company-level + round-only
   * companies) that {@link portfolio} is paged from — the number the page
   * paginates over. May differ from {@link portfolioCount} (the denormalized
   * headline total, which can include companies not yet resolvable to a card).
   */
  portfolioTotal: number;
  /** Funding rounds this investor led or participated in, newest first. */
  rounds: InvestorRoundRow[];
}

/** Minimal per-investor row for the sitemap. */
export interface InvestorSlugRow {
  slug: string;
  updated_at: string | null;
}

/** Row from the `news_articles` table, for the News section. */
export interface NewsArticleRow {
  id: string;
  url: string;
  title: string;
  source: string;
  published_date: string | null;
}

// ─── Relationship graph (similar / also-backed-by) ────────────────────────────

/**
 * A "similar" company from the `company_relationships` graph, joined with the
 * related company's display fields. Built in {@link getRelatedCompanies} from
 * the nested-select. `evidence` is the human-readable source/attribution string
 * shown as a muted caption (every fact on the page has a visible source).
 */
export interface RelatedCompany {
  slug: string;
  name: string;
  descriptionShort: string | null;
  status: string;
  industryGroup: string | null;
  score: number;
  evidence: string | null;
}

/**
 * A nearest-neighbor company by description-embedding cosine similarity,
 * returned by the `similar_companies` Postgres function (migration 0033) via
 * {@link getSimilarCompanies}. The function itself filters excluded companies
 * and rows without an embedding, so every entry here is renderable; the web
 * still drops rows with a missing slug/name defensively. `similarity` is
 * cosine similarity in roughly (0, 1], rendered as the per-card provenance
 * caption (every fact on the page has a visible source — this one's source is
 * the similarity computation itself).
 */
export interface SimilarCompany {
  slug: string;
  name: string;
  logoUrl: string | null;
  descriptionShort: string | null;
  industryGroup: string | null;
  similarity: number;
}

/**
 * A company that shares one or more (low-degree) investors with the company
 * being viewed, computed read-time in {@link getAlsoBackedBy}. `sharedInvestors`
 * holds the names of the shared investors, for the attribution caption.
 */
export interface AlsoBackedByCompany {
  slug: string;
  name: string;
  sharedInvestors: string[];
}

/**
 * Minimal row returned by {@link searchHuskFallback}: a company that matched
 * the search term by name but hasn't been enriched yet (husk). Surfaced below
 * the main result grid as a muted "we track this but have no full profile" link.
 */
export interface HuskFallbackRow {
  slug: string;
  name: string;
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

// ─── Compare view (Task C5) ───────────────────────────────────────────────────

/**
 * One company column in the /compare table. A flat, display-ready projection
 * built by {@link getCompaniesForCompare} — just the fields the side-by-side
 * comparison renders, so the page needs no per-company fan-out.
 */
export interface CompareCompany {
  slug: string;
  name: string;
  website: string | null;
  industryGroup: string | null;
  hqCity: string | null;
  hqState: string | null;
  status: string;
  yearIncorporated: number | null;
  employeeCountMin: number | null;
  employeeCountMax: number | null;
  /** Hybrid total: max(stated total_raised_usd, sum of known round amounts). */
  totalRaised: number | null;
  roundCount: number;
  latestRoundType: string | null;
  latestRoundAmount: number | null;
  latestRoundDate: string | null; // ISO date or null
  /** Distinct investor names (company-level + round-level), sorted, capped. */
  investors: string[];
  /** Top competitor names by rank, capped. */
  competitors: string[];
}

// ─── Co-investor signal (Task C5) ─────────────────────────────────────────────

/**
 * Another investor that frequently appears on the same funding rounds as the
 * investor being viewed, computed read-time in {@link getCoInvestors}.
 * `sharedRounds` is the number of rounds both backed (the co-investment count).
 */
export interface CoInvestor {
  slug: string;
  name: string;
  sharedRounds: number;
}

/** One heating-up portfolio company for the investor-page momentum lens. */
export interface PortfolioMomentumCompany {
  slug: string;
  name: string;
  momentumScore: number;
  momentumWhy: string[];
}

/**
 * Aggregate momentum across an investor's portfolio — the "which of this
 * investor's bets are accelerating right now" lens, computed read-time from the
 * companies' pipeline momentum_score (migration 0039 / #181). `scoredCount` is
 * the distinct shown portfolio companies that actually have a score; the
 * section is hidden when it's zero.
 */
export interface InvestorPortfolioMomentum {
  scoredCount: number;
  heatingUpCount: number;
  meanMomentum: number | null;
  topHeatingUp: PortfolioMomentumCompany[];
}

// ─── Themes (Wave 3 E-3) ───────────────────────────────────────────────────────

/**
 * Row from the `themes` table (migration 0034) as the /themes surfaces read
 * it. One row per named embedding cluster within an industry_group, written
 * replace-style by the pipeline's compute-themes stage. The funding columns
 * are build-time aggregates DERIVED from the member companies' stored
 * funding_rounds (trailing 2 complete calendar quarters vs the 2 before);
 * `funding_growth` is (recent − prior) / prior and NULL when prior is 0 —
 * the UI derives a "new funding" label from the sums instead.
 */
export interface ThemeListRow {
  slug: string;
  name: string;
  industry_group: string;
  description: string | null;
  company_count: number;
  funding_recent_usd: number;
  funding_prior_usd: number;
  funding_growth: number | null;
  updated_at: string;
}

/**
 * A theme member for the /themes/[slug] card grid: the CompanyCard
 * projection plus the membership's cosine similarity to the theme centroid
 * (the grid order + per-card ranking disclosure) and the company's
 * `created_at` (the "new entrants" list is the members most recently added
 * to the catalog).
 */
export interface ThemeMember extends CompanyListRow {
  similarity: number;
  created_at: string;
}

/** A member company's funding round as the theme page charts it. */
export interface ThemeRound {
  announced_date: string | null; // ISO date or null (undated: not chartable)
  amount_raised: number | null;
}

/** Everything /themes/[slug] renders, from {@link getThemeBySlug}. */
export interface ThemeDetail {
  theme: ThemeListRow;
  /** Shown members only (excluded companies are dropped), similarity desc. */
  members: ThemeMember[];
  /** The members' funding rounds — the quarter chart derives from these. */
  rounds: ThemeRound[];
}
