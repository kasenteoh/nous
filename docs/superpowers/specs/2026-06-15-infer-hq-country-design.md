# infer-hq-country — design

**Date:** 2026-06-15
**Status:** Approved (brainstorm), pending implementation plan
**Author:** Claude (CTO partner)

## Problem

The catalog is meant to list **US** software startups (spec §1.2 non-goal:
non-US companies). Country is resolved by a conservative three-tier rule in
`enrich-companies` / `judge-eligibility`: an explicit LLM statement → website
ccTLD → US-if-a-US-state/city-is-set, leaving `hq_country` NULL when there is
no positive evidence. A foreign company on a generic TLD (`.com` / `.io`) that
never states its location in the *scraped* text therefore lands with
`hq_country = NULL`, escapes the `non_us` exclusion, and stays "shown",
inflating coverage denominators.

Flagship example: **Fullview** (`fullview`, https://www.fullview.io/),
headquartered in Copenhagen — `hq_country` NULL, shown.

### What the investigation established (2026-06-14 PostgREST, read-only)

| Signal | Count |
|---|---|
| Shown companies (`exclusion_reason IS NULL` AND catalog bar) | 1,619 |
| …with `hq_country IS NULL` | 774 (~48%) |
| …already judged by the eligibility LLM, still NULL | 555 |
| …with a strong foreign legal suffix already in *stored* text | ~13 |

The scraper stores the homepage + the highest-scoring product/blog/about
links, **not** a guaranteed `/contact` or `/legal` page. The HQ signal is
usually absent from stored text: 555 companies were already run through the
eligibility LLM over their stored text and still came back NULL. Re-judging
*stored* text (or only tightening the prompt) would therefore recover a tiny
fraction and **would not catch Fullview** — verified directly: Fullview states
its HQ nowhere on its own site (homepage, `/contact`, `/about` all lack it; it
has no Privacy/Terms pages; the only hint is the soft `app.eu1.fullview.io`
region URL, which is not proof — US firms run EU instances too).

### Decision

Build a targeted, idempotent repair stage that **fetches the address-bearing
pages the homepage scraper skips** (`/about`, `/contact`, `/legal`,
`/imprint`, `/privacy`, …) on the company's *own* domain and runs a focused,
hardened country-inference LLM judgment over that text. This is the clean,
in-constraints lever: it stays on the company's own site (no new external
dependency, no paid search), every fact stays sourced, and it never fabricates.

**Accepted recall caveat:** this catches foreign companies that publish an
address / legal entity on their own site (the GmbH / Impressum / ApS /
"© …Ltd, <city>" population). It does **not** catch Fullview, which publishes
its HQ nowhere on its site. Fullview-type cases need either the deferred
web-search escalation (out of scope, would incur search cost) or a one-off
manual `exclude-company` call (see "Out of scope / follow-ups").

## Non-goals

- No external data sources (LinkedIn, Crunchbase, paid search). Own-site only.
- No re-scraping or re-judging of the whole catalog. Targeted to the
  shown + `hq_country IS NULL` population.
- Not run on the default cron — dispatch-gated only.
- Does not set US country loosely: US is set **only** on concrete quoted US
  evidence; otherwise the row is left NULL (US-plausible unknowns are left
  alone, per the task constraint).

## Architecture

A new pipeline stage `infer-hq-country`, mirroring the established
single-purpose, idempotent-stage conventions.

```
run_infer_hq_country(session_factory, client, *, limit, dry_run, db_op_timeout)
  selection (own short session):
    Company.exclusion_reason IS NULL
    AND Company.hq_country     IS NULL
    AND Company.website        IS NOT NULL
    AND Company.description_short IS NOT NULL     # enriched + shown
    AND Company.hq_country_checked_at IS NULL     # rotation / back-off
    ORDER BY hq_country_checked_at NULLS FIRST, name
    LIMIT :limit
  per company (FRESH session each, like judge-eligibility):
    1. load company + its stored RawPage text
    2. fetch ordered candidate paths on the company's own domain via
       HomepageClient (UA + robots.txt + 1 req/s enforced by the client);
       skip 404 / robots-blocked; stop after MAX_USABLE_PAGES with usable text
    3. cleaned = truncate(fetched_text_first + stored_text, MAX_PROMPT_INPUT_CHARS)
    4. judgment = complete_json(build_prompt(...), HqCountryJudgment)
    5. apply (see "Apply rules"); ALWAYS stamp hq_country_checked_at = now
    6. commit (bounded by db_op_timeout)
```

### Components

1. **Migration `0029`** — add `companies.hq_country_checked_at timestamptz
   NULL`, indexed. Exact mirror of the existing rotation-stamp columns
   (`news_checked_at`, `website_funding_checked_at`,
   `employee_count_checked_at`): drives selection ordering, back-off, and
   idempotency. `down_revision = 0028_latest_round_denorm`. Tested on a fresh
   `createdb` DB (parallel-worktree alembic-id collision note), not the shared
   `nous` DB.

2. **Prompt** `pipeline/src/nous/llm/prompts/hq_country.py`
   - `HqCountryJudgment(BaseModel)`:
     - `hq_country: str | None` — ISO-3166 alpha-2 (e.g. `"US"`, `"DK"`, `"GB"`).
     - `evidence_quote: str | None` — the verbatim snippet from the supplied
       text that establishes the country.
   - Lenient normalization (per the DeepSeek "normalize don't reject" lesson):
     a non-2-letter / unknown value coerces to `None`, never raises.
   - The prompt's single job is HQ country. It is hardened against the dominant
     false-positive trap on these pages — **customer / testimonial / investor /
     integration-partner names** (Fullview's page is wall-to-wall foreign
     customer logos): "Judge only THIS company's own headquarters. Ignore
     customers, testimonials, logos, investors, partners. Return null unless
     the text names the company's own city / country / registered address /
     legal entity. Never guess; quote the exact supporting text."

3. **Stage** `pipeline/src/nous/pipeline/infer_hq_country.py`
   - Candidate paths (ordered, deduped, joined to the site root):
     `/about`, `/about-us`, `/company`, `/contact`, `/contact-us`, `/legal`,
     `/imprint`, `/impressum`, `/privacy`, `/privacy-policy`, `/terms`, `/gdpr`.
   - `MAX_USABLE_PAGES` (≈4) and a max-paths-attempted cap bound the fetch
     count per company; a usable page = fetch ok AND
     `len(extract_visible_text(...)) >= MIN_TEXT_CHARS`.
   - Connection/rate-limit resilience copied from `judge_eligibility`:
     per-company fresh session, `db_op_timeout`-bounded DB ops, bounded
     `_safe_close`, **stop the loop** on `LLMRateLimitError`, skip-and-continue
     on `TimeoutError` / `LLMParseError` / `LLMError` / `StaleData` /
     `Integrity`.
   - `InferHqCountrySummary`: `companies_checked`, `excluded_non_us`,
     `set_us`, `left_unknown`, `fetch_failures`, `llm_failures`,
     `skipped_rate_limited`.

4. **CLI** — `nous infer-hq-country --limit N --dry-run`, mirroring the
   `judge-eligibility` command (build `HomepageClient` from
   `settings.SEC_USER_AGENT`, async session factory, log the summary).

5. **Dispatch gating** — `pipeline.yml`: new `run_infer_country` boolean input
   (default `false`) + `infer_country_limit` input (default `40`), plus a step
   `if: ${{ inputs.run_infer_country == true }}` running
   `uv run nous infer-hq-country --limit "$LIMIT"`. Modeled on
   `run_enrich_backfill`. **Not** added to the default cron.

## Apply rules (the no-fabrication core)

Let `cc = normalize_iso2(judgment.hq_country)` and `quote = judgment.evidence_quote`.

1. **Evidence guard.** Act on `cc` only if `quote` is non-empty AND, after
   whitespace+case normalization, is an actual substring of the supplied text.
   A paraphrased / hallucinated quote ⟶ treat as unknown. This guard errs
   toward *not* excluding, making a false exclusion of a US company
   structurally unlikely.
2. `cc` present, non-US, guard passed ⟶ `hq_country = cc`,
   `exclusion_reason = 'non_us'`,
   `exclusion_detail = 'HQ {cc} from {url}: "{quote}"'`, `excluded_at = now`.
3. `cc == 'US'`, guard passed (a concrete US city/state/address quote) ⟶
   `hq_country = 'US'`. (Legitimately shrinks the unknown denominator.)
4. Otherwise (no country, guard failed, ambiguous, silent — the Fullview case)
   ⟶ leave `hq_country` NULL.
5. **Always** stamp `hq_country_checked_at = now`. `--dry-run` logs intended
   actions and writes nothing.

## Idempotency & cost

- Re-running selects nothing once the population is stamped
  (`hq_country_checked_at IS NULL` filter + always-stamp). `--limit` +
  NULLS-FIRST ordering drains the backlog over successive dispatches.
- Per company: ≤ ~`MAX_USABLE_PAGES`-bounded fetches (own domain, throttled)
  + exactly one DeepSeek call. Bounded by `--limit`. Dispatch-gated, so prod
  spend is explicit (consistent with the remediation cost authorization).
- A row that yields no country is stamped and not retried — a site that does
  not state its HQ will not start doing so, and genuine new evidence still
  flows through the normal `enrich-companies` path.

## Testing (TDD; HomepageClient + complete_json mocked)

- **Apply matrix:** non-US+guard ⟶ sourced `non_us` exclusion; US+concrete
  quote ⟶ `hq_country='US'`; silent ⟶ NULL + stamp; always-stamp;
  `--dry-run` writes nothing.
- **Evidence guard:** paraphrased quote not in source ⟶ no action (NULL).
- **Customer trap:** text full of foreign customer names, own HQ unstated ⟶
  NULL.
- **Fullview-like:** no on-site location signal ⟶ NULL + stamped (not retried).
- **EU-address:** `© Acme GmbH, Berlin, Germany` ⟶ `DE`, excluded, sourced.
- **Fetch resilience:** 404 / robots-blocked candidate paths skipped; falls
  back to stored text; rate-limit stops the loop.
- **Units:** candidate-path building, ISO2 coercion, evidence-substring
  normalization.
- Full verify before done: `ruff check`, `mypy src`, `pytest` (pipeline) +
  `npm run build` (web — unaffected, run per CLAUDE.md).

## Out of scope / follow-ups

- **Fullview specifically:** not catchable on-site. Optional one-off:
  `nous exclude-company fullview --reason non_us --detail "Copenhagen, Denmark
  (external)"` via dispatch — sourced to external knowledge, separate from the
  automated stage. Flag for the user.
- **Web-search escalation (Approach B):** deferred — needs a paid/fragile
  search source; would incur cost (flag before implementing).
- **Tightening the eligibility prompt** for future enrichments is a cheap
  complementary win but secondary; can fold the customer-trap wording into
  `company_eligibility.py` in a later pass.
