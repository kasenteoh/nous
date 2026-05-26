# nous — Technical Specification

**Version:** 0.1 (initial spec for Claude Code)
**Status:** Pre-implementation
**Audience:** Claude Code, for decomposition into discrete coding tasks

---

## 1. Product Overview

**nous** is a free, public-facing website for discovering and reading about US software startups. It is positioned as a more readable, more detailed alternative to Crunchbase for the subset of startups that have raised institutional capital recently.

### 1.1 Goals

1. Index every US software startup that filed a Form D with the SEC in the trailing 12 months.
2. For each startup, present:
   - A short product summary (one or two sentences)
   - A long-form product writeup (several paragraphs, the kind of thing a curious reader would actually enjoy)
   - Estimated valuation, when derivable from public sources, with attribution
   - Rough employee count (a range, with source)
   - Funding history with round size, date, lead investor, and other participating investors
   - Competitor analysis: a list of likely competitors, each with a brief description and reasoning
3. Refresh data weekly.
4. Operate entirely on free tiers (no paid LLM APIs, no paid hosting, no paid data sources).

### 1.2 Non-Goals (v1)

- Non-US companies
- Non-software companies
- Companies that have not filed a Form D in the last 12 months
- User accounts, comments, or any social features
- Real-time updates (weekly is sufficient)
- Mobile-native apps

---

## 2. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend / Data pipeline | Python 3.11+ | Best ecosystem for scraping, SEC parsing, data work |
| Package management | `uv` | Fast, modern, simple |
| Database | Postgres 15 | Hosted on Supabase free tier (500MB) |
| ORM | SQLAlchemy 2.x + Alembic | Mature, type-safe |
| Web framework (data) | None for pipeline. Pipeline is scripts. | Pipeline runs as scheduled jobs, not a service |
| Frontend | Next.js 16 (App Router) | Server components, SEO-friendly, free Vercel hosting. `params` is a Promise in async page components — see `web/AGENTS.md`. |
| Frontend styling | Tailwind CSS | Standard, fast iteration |
| Frontend hosting | Vercel (free tier) | Auto-deploy from GitHub |
| Scheduling | GitHub Actions (cron) | Free for public repos, 2000 min/month |
| LLM provider | Google Gemini 2.5 Flash (free tier) | Generous free tier (~1500 req/day), good quality for extraction |
| LLM client abstraction | LiteLLM or thin custom wrapper | Allows swapping providers via config |
| HTTP scraping | `httpx` + `selectolax` | Async-capable, fast HTML parsing |
| News source | Google News RSS | Free, no API key |
| Frontend deploy DB connection | Supabase JS client (server-side only) | Avoids exposing DB credentials |

### 2.1 Repository Layout

```
nous/
├── pipeline/                    # Python data pipeline
│   ├── pyproject.toml
│   ├── alembic/                 # DB migrations
│   ├── src/
│   │   └── nous/
│   │       ├── __init__.py
│   │       ├── config.py        # Settings via pydantic-settings
│   │       ├── db/
│   │       │   ├── models.py    # SQLAlchemy models
│   │       │   └── session.py
│   │       ├── sources/
│   │       │   ├── edgar.py     # SEC EDGAR client
│   │       │   ├── homepage.py  # Homepage scraper
│   │       │   └── news.py      # Google News RSS client
│   │       ├── llm/
│   │       │   ├── client.py    # LLM provider abstraction
│   │       │   └── prompts/     # Prompt templates
│   │       ├── pipeline/
│   │       │   ├── ingest_filings.py
│   │       │   ├── enrich_companies.py
│   │       │   ├── ingest_news.py
│   │       │   ├── extract_funding.py
│   │       │   └── analyze_competitors.py
│   │       └── cli.py           # Entrypoint for cron jobs
│   └── tests/
├── web/                         # Next.js frontend
│   ├── package.json
│   ├── app/
│   │   ├── layout.tsx
│   │   ├── page.tsx             # Index / browse
│   │   ├── c/[slug]/page.tsx    # Company detail
│   │   └── api/                 # Search endpoint, if needed
│   ├── components/
│   ├── lib/
│   │   └── db.ts                # Supabase client
│   └── tailwind.config.ts
├── .github/
│   └── workflows/
│       ├── weekly-pipeline.yml
│       └── deploy-web.yml
└── README.md
```

---

## 3. Data Sources

### 3.1 SEC EDGAR (primary spine)

- **Form D filings** are public, structured, and free.
- Full-text search endpoint: `https://efts.sec.gov/LATEST/search-index?q=&forms=D&dateRange=custom&startdt=YYYY-MM-DD&enddt=YYYY-MM-DD`
- Submissions API: `https://data.sec.gov/submissions/CIK{cik}.json`
- Per-filing data: `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/primary_doc.xml`
- **Required:** `User-Agent` header with contact email, e.g. `nous-project contact@example.com`
- **Rate limit:** SEC asks for no more than 10 requests/second.

#### Filtering software companies

Form D uses an `industryGroupType` field. Filter to:
- `Technology - Computers`
- `Technology - Other`
- (Optionally) `Technology - Telecommunications` if it overlaps with software meaningfully

Implementation should make this filter list configurable.

#### Fields to capture from Form D

- `accessionNumber`
- `cik`
- `entityName`
- `industryGroupType`
- `yearOfIncorporation`
- `principalPlaceOfBusiness` (city, state, country)
- `entityType` (LLC, Corporation, etc.)
- `totalOfferingAmount`
- `totalAmountSold`
- `totalRemaining`
- `minimumInvestmentAccepted`
- `totalNumberAlreadyInvested` (investor count)
- `relatedPersonsList` (name, relationship, address) — these are directors/officers/executives, sometimes named investors

### 3.2 Company homepages

- Resolve homepage URL: try common patterns (`{normalized_name}.com`, `.io`, `.ai`, `.co`), then fall back to a Google web search via DuckDuckGo HTML (free, scrapable) or Bing free tier.
- Fetch homepage + likely subpages: `/`, `/about`, `/about-us`, `/product`, `/products`, `/company`, `/team`.
- Respect `robots.txt`.
- Cache aggressively (refetch quarterly, not weekly).
- Store raw HTML so we can re-extract without re-scraping.

### 3.3 News articles

- Use Google News RSS: `https://news.google.com/rss/search?q=%22{company_name}%22+funding&hl=en-US&gl=US`
- Restrict to last 12 months.
- Fetch and store article content via `httpx` + `selectolax`.
- For valuation extraction, prioritize reputable sources: TechCrunch, Axios, Reuters, Bloomberg (when accessible), The Information (public posts), Forbes, Business Insider, Fortune, Wired.
- Deduplicate articles by URL canonical form.

### 3.4 Employee count signals

In order of preference:
1. Wellfound (AngelList) public profile, if findable. Public profiles expose employee ranges.
2. theorg.com public profile.
3. growjo.com.
4. Open job count on careers page (proxy for company size).
5. GitHub organization member count (for dev-tool companies).

Each signal should store the source so the frontend can render attribution.

### 3.5 Sources explicitly NOT used

- LinkedIn (scraping violates ToS, they litigate aggressively)
- Crunchbase (paywalled, ToS prohibits scraping)
- PitchBook, CB Insights, Tracxn (paid)

---

## 4. Database Schema

All tables use UUIDs for primary keys, plus `created_at` and `updated_at` timestamps.

### 4.1 `companies`

| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| cik | text | SEC CIK, unique, nullable (in case of merge artifacts) |
| name | text | Canonical name from Form D |
| slug | text | URL-safe, unique, e.g. `acme-software` |
| normalized_name | text | Lowercased, stripped of suffixes (Inc., LLC, etc.) |
| description_short | text | LLM-generated, ~1-2 sentences |
| description_long | text | LLM-generated, ~3-6 paragraphs, markdown |
| website | text | Resolved homepage URL |
| logo_url | text | Optional, scraped from homepage `<link rel="icon">` or og:image |
| hq_city | text | |
| hq_state | text | |
| hq_country | text | Default 'US' |
| year_incorporated | int | |
| industry_group | text | From Form D |
| employee_count_min | int | nullable |
| employee_count_max | int | nullable |
| employee_count_source | text | e.g. "wellfound", "theorg", "github_org" |
| last_enriched_at | timestamptz | When LLM enrichment last ran |
| created_at | timestamptz | |
| updated_at | timestamptz | |

### 4.2 `filings`

| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| company_id | uuid | FK companies |
| accession_number | text | unique |
| filing_date | date | |
| offering_amount_total | numeric | |
| amount_sold | numeric | |
| investors_count | int | |
| minimum_investment | numeric | |
| raw_data | jsonb | Full parsed Form D XML, for future re-extraction |
| created_at | timestamptz | |

### 4.3 `funding_rounds`

A funding round is the user-facing concept. It may be derived from a filing, from news, or both (joined where possible).

| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| company_id | uuid | FK companies |
| round_type | text | "Pre-Seed", "Seed", "Series A", "Series B", etc. |
| amount_raised | numeric | nullable |
| valuation_post_money | numeric | nullable |
| valuation_source | text | e.g. "TechCrunch, Mar 2025" |
| announced_date | date | nullable |
| filing_id | uuid | FK filings, nullable |
| primary_news_url | text | The article most relied on |
| created_at | timestamptz | |

### 4.4 `investors`

| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| name | text | unique on lowercased form |
| type | text | "institutional" \| "angel" \| "unknown" |
| description | text | nullable |
| website | text | nullable |

### 4.5 `funding_round_investors` (join table)

| Column | Type | Notes |
|---|---|---|
| funding_round_id | uuid | FK |
| investor_id | uuid | FK |
| is_lead | boolean | default false |

### 4.6 `competitors`

| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| company_id | uuid | The company this competitor list belongs to |
| competitor_company_id | uuid | FK companies, nullable (if competitor is in our DB) |
| competitor_name | text | Always populated |
| description | text | 1-2 sentences |
| reasoning | text | Why the LLM thinks they compete |
| rank | int | Ordering, 1 = most direct competitor |

### 4.7 `news_articles`

| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| company_id | uuid | FK companies |
| url | text | unique (canonical form) |
| title | text | |
| source | text | e.g. "techcrunch.com" |
| published_date | date | nullable |
| raw_content | text | Article body |
| processed | boolean | Set true after funding extraction |
| created_at | timestamptz | |

### 4.8 `raw_pages` (homepage / about page cache)

| Column | Type | Notes |
|---|---|---|
| id | uuid | PK |
| company_id | uuid | FK companies |
| url | text | |
| content | text | Raw HTML |
| fetched_at | timestamptz | |

### 4.9 Indexes

- `companies.slug` unique
- `companies.cik` unique (allow null)
- `filings.accession_number` unique
- `funding_rounds.company_id`
- `news_articles.url` unique
- Full-text index on `companies.name`, `companies.description_short`, `companies.description_long` for search

---

## 5. Pipeline Stages

The pipeline is a sequence of idempotent stages. Each stage is a CLI command invoked by `python -m nous.cli {stage}`. The weekly GitHub Action runs them in order.

### 5.1 Stage 1: Ingest Filings (`ingest-filings`)

- Query EDGAR for all Form D filings with `industryGroupType` in the configured set, filed in the last 7 days (with a 14-day overlap buffer for safety).
- For each new `accession_number`:
  - Parse the Form D XML.
  - Upsert the `companies` row (matched by CIK, or by normalized name as fallback).
  - Insert a `filings` row.
  - Insert `related_persons` records.

### 5.2 Stage 2: Resolve Homepages (`resolve-homepages`)

- For companies without a `website` value:
  - Try common URL patterns.
  - Fall back to DuckDuckGo HTML search.
  - Validate the resolved domain looks plausible (does the page mention the company name?).
- For companies with a `website` older than 90 days, re-fetch.

### 5.3 Stage 3: Scrape Homepages (`scrape-homepages`)

- For each company needing scraping:
  - Fetch `/`, `/about`, `/product`, `/team`, `/company`.
  - Store raw HTML in `raw_pages`.
- Respect robots.txt and a 1 req/sec per-domain throttle.

### 5.4 Stage 4: LLM Enrichment (`enrich-companies`)

For each company with new `raw_pages` and no recent enrichment:
- Concatenate cleaned text from all scraped pages (strip nav, footer, scripts).
- Call LLM with the company-description prompt (see §6.1).
- Update `companies.description_short`, `description_long`, `last_enriched_at`.

### 5.5 Stage 5: Ingest News (`ingest-news`)

- For each company, query Google News RSS for the last 7 days.
- Filter to articles whose title or snippet mentions a funding-related keyword: `raised`, `funding`, `seed`, `series`, `valuation`, `closes`, `led by`.
- Fetch full article content for matching URLs.
- Store in `news_articles`.

### 5.6 Stage 6: Extract Funding (`extract-funding`)

For each unprocessed news article:
- Call LLM with the funding-extraction prompt (see §6.2).
- LLM returns structured JSON: round type, amount, valuation, lead, other investors, date.
- Reconcile with existing `funding_rounds` (match by company + announced_date proximity).
- Upsert `funding_rounds`, `investors`, `funding_round_investors`.
- Mark article `processed = true`.

### 5.7 Stage 7: Analyze Competitors (`analyze-competitors`)

For each company without competitor data, or whose enrichment changed:
- Build a context blob: company description, industry group, list of other companies in nous in the same industry group.
- Call LLM with the competitor-analysis prompt (see §6.3).
- LLM returns ranked list of competitors with descriptions and reasoning.
- Where competitor name matches a company already in nous, link `competitor_company_id`.

### 5.8 Stage 8: Estimate Employees (`estimate-employees`)

For each company without recent employee data:
- Try public sources in order (Wellfound, theorg, growjo, careers page job count, GitHub org).
- Store the range and source.

### 5.9 Idempotency

Every stage should be safe to re-run. Use upserts keyed on natural unique identifiers (accession_number, news URL, etc.). The pipeline should never throw on duplicate data.

---

## 6. LLM Prompts

All prompts return JSON. The LLM client validates output against a Pydantic model and retries once on parse failure.

### 6.1 Company description prompt

**Input:** Cleaned text from homepage + about/product pages (truncate to ~8K tokens).

**Output schema:**
```json
{
  "description_short": "string, 1-2 sentences, plain language, no marketing fluff",
  "description_long": "string, 3-6 paragraphs of markdown, detailed but readable, covers: what the product does, who it is for, how it works, what makes it distinctive",
  "primary_category": "string, e.g. 'developer tools', 'fintech', 'AI infrastructure'",
  "tags": ["array", "of", "strings"]
}
```

**Prompt guidelines:**
- Strip marketing language. Write like a curious analyst, not a press release.
- If the page is thin or unclear, say so in the description (do not invent).
- The long version should be the kind of thing the user described as "what they enjoy reading."

### 6.2 Funding extraction prompt

**Input:** Full text of a news article + company name.

**Output schema:**
```json
{
  "is_funding_announcement": "boolean",
  "round_type": "string or null, e.g. 'Series A'",
  "amount_raised_usd": "number or null",
  "valuation_post_money_usd": "number or null",
  "announced_date": "ISO date or null",
  "lead_investors": ["array of strings"],
  "other_investors": ["array of strings"],
  "confidence": "low | medium | high"
}
```

If `is_funding_announcement` is false, the article is skipped.

### 6.3 Competitor analysis prompt

**Input:**
- Target company name + short + long description
- A list of up to 50 other companies in nous in the same `industry_group` (name + short description)

**Output schema:**
```json
{
  "competitors": [
    {
      "name": "string",
      "description": "string, 1-2 sentences",
      "reasoning": "string, why they compete with the target",
      "rank": "integer, 1 = most direct"
    }
  ]
}
```

**Prompt guidelines:**
- Prefer competitors from the provided list of indexed companies where reasonable.
- May include well-known competitors not in the list (e.g. obvious incumbents).
- Return up to 6 competitors.
- Do not invent fictional companies.

---

## 7. Frontend

### 7.1 Routes

| Path | Purpose |
|---|---|
| `/` | Browse / search index |
| `/c/[slug]` | Company detail page |
| `/about` | About nous (single static page) |

### 7.2 Index page (`/`)

- Hero with the name "nous" and a one-liner tagline.
- Filter controls: industry group, recency of last funding, round size bucket.
- Search box (full-text against company name + description).
- Paginated grid of company cards. Each card: name, short description, latest round type + amount, location.
- Server-rendered via Next.js server components, reading from Supabase via the server-side client.

### 7.3 Company detail page (`/c/[slug]`)

Sections in order:
1. **Header:** name, location, year founded, website link, logo.
2. **Short description.**
3. **Key facts strip:** estimated valuation (with attribution), employee range (with source), total raised, last round.
4. **Long description** (rendered markdown).
5. **Funding history table:** each round with date, type, amount, valuation, lead, other investors.
6. **Competitors section:** each competitor as a card with name, description, reasoning. Cards link internally if the competitor is indexed in nous.
7. **Sources footer:** list of news articles and filings used to construct this page, with links.

### 7.4 Styling

- Tailwind CSS.
- Clean, text-forward, readable. Think more like Stratechery or Read Max than Crunchbase.
- Light/dark mode toggle, system default.
- No client-side JS beyond the search and filter controls.

### 7.5 Data fetching

- Server components query Supabase directly using the Supabase JS client with the service role key (server-only).
- Pages are statically generated at build time, with `revalidate` set to 6 hours.
- The weekly GitHub Action that runs the pipeline triggers a Vercel deploy hook after completing.

---

## 8. Deployment & Operations

### 8.1 Environments

- **Production:** `nous.vercel.app` (or custom domain when ready).
- **Local dev:** Postgres via Docker, Next.js dev server, pipeline runs against local DB.

### 8.2 Secrets (stored in GitHub Actions secrets and Vercel env vars)

- `DATABASE_URL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `GEMINI_API_KEY`
- `SEC_USER_AGENT` (e.g. `nous-project contact@yourdomain.com`)
- `VERCEL_DEPLOY_HOOK_URL`

### 8.3 GitHub Actions

**Workflow: `weekly-pipeline.yml`**
- Cron: every Monday at 09:00 UTC
- Steps:
  1. Checkout
  2. Install Python + `uv`
  3. Install pipeline deps
  4. Run migrations
  5. Run each pipeline stage in order
  6. POST to Vercel deploy hook

**Workflow: `deploy-web.yml`**
- Trigger: push to `main` with changes under `web/`
- Vercel handles the actual deploy via its GitHub integration; this workflow runs only lint/typecheck.

### 8.4 Monitoring

- Pipeline writes a summary log at the end of each run (counts: new companies, new filings, articles processed, LLM calls used).
- Failures surface as failed GitHub Action runs.
- No paid observability (it is fine to revisit later).

---

## 9. Build Order (Milestones)

Each milestone should be independently shippable.

### Milestone 1: Spine
**Goal:** Form D ingestion working end-to-end with a bare-bones company page.

- Set up monorepo (`pipeline/` and `web/`).
- Configure Supabase project, create initial schema (companies, filings, related_persons).
- Implement EDGAR client + Form D parser.
- Implement `ingest-filings` stage.
- Backfill the last 30 days of filings as an initial test.
- Build Next.js scaffolding with index page (list of companies) and `/c/[slug]` showing only Form D data.
- Deploy to Vercel.

### Milestone 2: Homepage scraping + LLM descriptions
- Implement homepage resolution and scraping.
- Add LLM client with Gemini.
- Implement `enrich-companies` stage.
- Update company page to show short and long descriptions.

### Milestone 3: News + funding extraction
- Implement Google News RSS client.
- Implement `ingest-news` and `extract-funding` stages.
- Add funding history table to the company page.

### Milestone 4: Competitor analysis
- Implement `analyze-competitors` stage.
- Add competitors section to the company page.

### Milestone 5: Employee estimation + polish
- Implement employee count signals.
- Add search, filters, sorting on the index page.
- Polish styling.
- Add `/about` page.

### Milestone 6: Weekly automation
- Backfill last 12 months of filings (run in batches across multiple days if needed for free-tier limits).
- Wire up the weekly GitHub Action.
- Set up Vercel deploy hook.

---

## 10. Open Questions to Revisit

1. **Logos.** Scraping favicons or og:image works but quality is uneven. Worth revisiting in M5.
2. **Slug collisions.** Two companies named "Acme Inc" need disambiguation. Suggest: `{slug}-{short-hash}` on collision.
3. **Defunct companies.** A company filed 11 months ago and shut down 2 months ago. We have no signal. Acceptable for v1; revisit later.
4. **Rate-limit handling for Gemini free tier.** If we hit limits during backfill, the pipeline should resume gracefully on the next run.
5. **Bot detection on news sites.** Some publishers serve different content to scrapers. Acceptable to skip articles we cannot read.

---

## 11. Constraints & Principles

- **Free tier first.** Every component should fit on free tiers. If anything starts costing money, surface it.
- **Idempotent stages.** Re-running a stage should never produce duplicates or corrupted data.
- **Source attribution.** Every derived fact (valuation, employee count, description) should have a source we can show.
- **No fabrication.** The LLM is instructed to return null rather than guess. If a field is unknown, the frontend shows it as unknown.
- **Respect ToS and robots.txt.** We are building on public data; behave like a good citizen.

---

*End of spec.*
